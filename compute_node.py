import glob
import sys
sys.path.append('gen-py')
sys.path.insert(0, glob.glob('/home/hsu00191/Distributed_Systems/thrift-0.19.0/lib/py/build/lib*')[0])

import time
import random
import numpy as np
from thrift.transport import TSocket, TTransport
from thrift.protocol import TBinaryProtocol
from thrift.server import TServer

from service.ComputeNode import Iface
from shared.ttypes import TaskStatus, MLModel, MLGradient, TrainingResult
from service import ComputeNode
from ML.ML import mlp, calc_gradient

class ComputeNodeHandler(Iface):
    def __init__(self, load_probability):
        self.load_probability = load_probability
        self.mlp_model = mlp()

    def _inject_load(self):
        """ Simulate load by sleeping with probability """
        if random.random() < self.load_probability:
            time.sleep(3)  # Simulating load

    def _should_accept_task(self):
        """ Decide if task should be accepted based on load probability """
        return random.random() >= self.load_probability

    def initializeTraining(self, filename, model):
        """ Initializes the MLP model with given weights """
        if not self._should_accept_task():
            return TaskStatus.REJECTED  # Reject if overloaded

        self._inject_load()

        V = np.array(model.V)
        W = np.array(model.W)
        success = self.mlp_model.init_training_model(filename, V, W)

        return TaskStatus.ACCEPTED if success else TaskStatus.REJECTED

    def trainModel(self, eta, epochs):
        """ Trains the model and returns the computed gradient """

        # Store original weights before training
        V_old, W_old = self.mlp_model.get_weights()

        # Train the model
        error_rate = self.mlp_model.train(eta, epochs)

        # Get updated weights after training
        V_new, W_new = self.mlp_model.get_weights()

        # Compute gradients: gradient = new_weights - old_weights
        dV = calc_gradient(V_new, V_old)
        dW = calc_gradient(W_new, W_old)

        print(f"[DEBUG] Compute Node Gradient - dW sum: {np.sum(dW)}, dV sum: {np.sum(dV)}")


        # Convert to Thrift struct
        gradient = MLGradient(dV=dV.tolist(), dW=dW.tolist())

        return TrainingResult(gradient=gradient, error_rate=error_rate)



def main():
    if len(sys.argv) != 3:
        print("Usage: python3 compute_node.py <port> <load_probability>")
        sys.exit(1)

    port = int(sys.argv[1])
    load_probability = float(sys.argv[2])

    handler = ComputeNodeHandler(load_probability)
    processor = ComputeNode.Processor(handler)
    transport = TSocket.TServerSocket(port=port)
    tfactory = TTransport.TBufferedTransportFactory()
    pfactory = TBinaryProtocol.TBinaryProtocolFactory()

    server = TServer.TSimpleServer(processor, transport, tfactory, pfactory)

    print(f"[STARTED] Compute Node listening on port {port} with load probability {load_probability}")
    server.serve()

if __name__ == "__main__":
    main()
