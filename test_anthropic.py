from neural_network_manager import NeuralNetworkManager

mgr = NeuralNetworkManager()
print('provider=', mgr.provider)
print(mgr.generate_response('Кратко представься на русском, 1-2 предложения.', max_tokens=60))
