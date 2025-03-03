import glob
import sys
sys.path.append('gen-py')
sys.path.insert(0, glob.glob('../thrift-0.19.0/lib/py/build/lib*')[0])

import random
import threading
import numpy as np
from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol
from thrift.server import TServer

from service.Coordinator import Iface
from shared.ttypes import TaskStatus, MLModel, TrainingResult
from service import ComputeNode
from service import Coordinator
from ML.ML import mlp, scale_matricies, sum_matricies

# Thread-safe shared gradient storage
class SharedGradient:
    def __init__(self, shape):
        self.gradient = np.zeros(shape)
        self.lock = threading.Lock()

    def update(self, local_gradient):
        with self.lock:
            self.gradient = sum_matricies(self.gradient, local_gradient)

    def average(self, num_jobs):
        with self.lock:
            return scale_matricies(self.gradient, 1 / max(num_jobs, 1))  # Prevent division by zero

    def reset(self):
        with self.lock:
            self.gradient = np.zeros_like(self.gradient)

class CoordinatorHandler(Iface):
    def __init__(self, scheduling_policy, compute_nodes_file):
        self.scheduling_policy = scheduling_policy
        self.mlp_model = mlp()
        self.compute_nodes = self._load_compute_nodes(compute_nodes_file)
        self.node_load = {node: 0 for node in self.compute_nodes}  # Tracks active jobs per node
        self.lock = threading.Lock()  # Ensure thread safety when modifying `node_load`

    def _select_compute_node(self):
        """ Selects a compute node based on the scheduling policy """
        if self.scheduling_policy == 1:  
            # TODO attempt to acquire node
            return random.choice(self.compute_nodes)  # Randomly schedule a node
        else:  
            with self.lock:
                # TODO attempt to acquire node
                return min(self.node_load, key=self.node_load.get)  # Get the node with the lowest load

    def _increment_node_load(self, node):
        """ Increment the job count for a node """
        with self.lock:
            self.node_load[node] += 1

    def _decrement_node_load(self, node):
        """ Decrement the job count for a node """
        with self.lock:
            self.node_load[node] = max(0, self.node_load[node] - 1)

    def thread_func(self, node_host, node_port, training_file, shared_gradient_V, shared_gradient_W, V, W, eta, epochs):
        """ Worker thread for training a single batch """
        node = (node_host, node_port)
        # TODO: try to acquire node. if fails, reschedule?
        # poll nodes:
        # self._acquire_node()
        # 
        self._increment_node_load(node)  # Mark the node as handling a job

        try:
            transport = TSocket.TSocket(node_host, node_port)
            transport = TTransport.TBufferedTransport(transport)
            protocol = TBinaryProtocol.TBinaryProtocol(transport)
            client = ComputeNode.Client(protocol)
            transport.open()

            model = MLModel(W=W.tolist(), V=V.tolist())
            status = client.initializeTraining(training_file, model)

            if status == TaskStatus.ACCEPTED:
                # TODO time
                result = client.trainModel(eta, epochs)
                # TODO end time
                local_gradient_V = np.array(result.gradient.dV)
                local_gradient_W = np.array(result.gradient.dW)
                print(f"[DEBUG] Received Gradients: dW sum: {np.sum(np.abs(local_gradient_W))}, dV sum: {np.sum(np.abs(local_gradient_V))}")

                shared_gradient_V.update(local_gradient_V)
                shared_gradient_W.update(local_gradient_W)

            transport.close()

        except Exception as e:
            print(f"[ERROR] Compute node {node_host}:{node_port} failed - {e}")

        finally:
            self._decrement_node_load(node)  # Mark the job as complete

    def _load_compute_nodes(self, filename):
        """ Reads compute nodes from file and returns a list of (host, port) tuples """
        nodes = []
        try:
            with open(filename, "r") as file:
                for line in file:
                    host, port = line.strip().split(",")
                    nodes.append((host, int(port)))
            print(f"[INFO] Loaded {len(nodes)} compute nodes.")
        except Exception as e:
            print(f"[ERROR] Failed to load compute nodes from {filename}: {e}")
        return nodes

    def train(self, dir, rounds, epochs, h, k, eta):
        """
        Runs distributed training over multiple rounds using the compute nodes.
        - dir: Directory containing training/validation data
        - rounds: Number of training rounds
        - epochs: Training epochs per round
        - h, k: Hidden & output layer sizes
        - eta: Learning rate
        """
        # Initialize model with random weights
        success = self.mlp_model.init_training_random(f"{dir}/train_letters1.txt", k, h)
        if not success:
            print("[ERROR] MLP model initialization failed. Check dataset path.")
            return -1
        
        V, W = self.mlp_model.get_weights()
        print(f"[DEBUG] Initial Weights: W shape {W.shape}, V shape {V.shape}")

        shared_gradient_V = SharedGradient(V.shape)
        shared_gradient_W = SharedGradient(W.shape)

        for r in range(rounds):
            print(f"[TRAINING ROUND {r+1}/{rounds}]")
            # TODO start time for round

            # Retrieve latest weights and reset gradients
            V, W = self.mlp_model.get_weights()
            shared_gradient_V.reset()
            shared_gradient_W.reset()

            threads = []
            # Change this back to process all files
            work_queue = [f"{dir}/train_letters{i}.txt" for i in range(1, 12)]

            for training_file in work_queue:
                node_host, node_port = self._select_compute_node()
                t = threading.Thread(
                    target=self.thread_func, 
                    args=(node_host, node_port, training_file, shared_gradient_V, shared_gradient_W, V, W, eta, epochs)
                )
                threads.append(t)
                t.start()

            for t in threads:
                t.join()
            
            avg_gradient_V = shared_gradient_V.average(len(work_queue))
            avg_gradient_W = shared_gradient_W.average(len(work_queue))

            if avg_gradient_W.shape == W.shape and avg_gradient_V.shape == V.shape:
                
                # check to see if weights are being updated, then update them:
                print(f"[DEBUG] Avg Absolute Gradients: dW sum {np.sum(np.abs(avg_gradient_W))}, dV sum {np.sum(np.abs(avg_gradient_V))}")
                self.mlp_model.update_weights(avg_gradient_V, avg_gradient_W)
                
                # Verify weights were updated
                new_V, new_W = self.mlp_model.get_weights()
                # print(f"[DEBUG] Updated Weights: W {new_W[:2]}, V {new_V[:2]}")
                
            else:
                print("[ERROR] Gradient shapes do not match. Skipping update.")
            
            # Validate model after each round
            val_error = self.mlp_model.validate(f"{dir}/train_letters11.txt")
            print(f"[VALIDATION ERROR] After round {r+1}: {val_error:.4f}")
            
            # TODO print end time for round

        # TODO time
        return val_error

def main():
    """ Main entry point for starting the coordinator server """
    if len(sys.argv) != 3:
        print("Usage: python3 coordinator.py <port> <scheduling_policy>")
        sys.exit(1)

    port = int(sys.argv[1])
    scheduling_policy = int(sys.argv[2])

    handler = CoordinatorHandler(scheduling_policy, "compute_nodes.txt")
    processor = Coordinator.Processor(handler)
    transport = TSocket.TServerSocket(port=port)
    tfactory = TTransport.TBufferedTransportFactory()
    pfactory = TBinaryProtocol.TBinaryProtocolFactory()

    server = TServer.TSimpleServer(processor, transport, tfactory, pfactory)

    print(f"[STARTED] Coordinator listening on port {port} with scheduling policy {scheduling_policy}")
    server.serve()

if __name__ == "__main__":
    main()
