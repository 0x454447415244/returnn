

from nose.tools import assert_equal, assert_is_instance, assert_in
from NetworkDescription import LayerNetworkDescription
from Config import Config
from Network import LayerNetwork
from Util import dict_diff_str
from pprint import pprint


def test_init():
  n_in = 5
  n_out = {"classes": (10, 1)}
  desc = LayerNetworkDescription(
    num_inputs=n_in, num_outputs=n_out,
    hidden_info=[],
    output_info={},
    default_layer_info={})

  assert_equal(desc.num_inputs, n_in)
  assert_equal(desc.num_outputs, n_out)


def test_num_inputs_outputs_old():
  n_in = 5
  n_out = 10
  config = Config()
  config.update({"num_inputs": n_in, "num_outputs": n_out})
  num_inputs, num_outputs = LayerNetworkDescription.num_inputs_outputs_from_config(config)
  assert_equal(num_inputs, n_in)
  assert_is_instance(num_outputs, dict)
  assert_equal(len(num_outputs), 1)
  assert_in("classes", num_outputs)
  assert_equal(num_outputs["classes"], [n_out, 1])


config1_dict = {
  "num_inputs": 5,
  "num_outputs": 10,
  "hidden_size": (7, 8,),
  "hidden_type": "hidden",
  "activation": "relu",
  "bidirectional": False,
}


def test_config1_basic():
  config = Config()
  config.update(config1_dict)
  desc = LayerNetworkDescription.from_config(config)
  assert_is_instance(desc.hidden_info, list)
  assert_equal(len(desc.hidden_info), len(config1_dict["hidden_size"]))
  assert_equal(desc.num_inputs, config1_dict["num_inputs"])


def test_NetworkDescription_to_json_config1():
  config = Config()
  config.update(config1_dict)
  desc = LayerNetworkDescription.from_config(config)
  orig_network = LayerNetwork.from_description(desc)
  orig_json_content = orig_network.to_json_content()
  desc_json_content = desc.to_json_content()
  pprint(desc_json_content)
  new_network = LayerNetwork.from_json(
    desc_json_content,
    config1_dict["num_inputs"],
    {"classes": (config1_dict["num_outputs"], 1)})
  new_json_content = new_network.to_json_content()
  if orig_json_content != new_json_content:
    print(dict_diff_str(orig_json_content, new_json_content))
    assert_equal(orig_json_content, new_network.to_json_content())


def test_config1_to_json_network_copy():
  config = Config()
  config.update(config1_dict)
  orig_network = LayerNetwork.from_config_topology(config)
  orig_json_content = orig_network.to_json_content()
  pprint(orig_json_content)
  new_network = LayerNetwork.from_json(orig_json_content, orig_network.n_in, orig_network.n_out)
  assert_equal(orig_network.n_in, new_network.n_in)
  assert_equal(orig_network.n_out, new_network.n_out)
  new_json_content = new_network.to_json_content()
  if orig_json_content != new_json_content:
    print(dict_diff_str(orig_json_content, new_json_content))
    assert_equal(orig_json_content, new_network.to_json_content())