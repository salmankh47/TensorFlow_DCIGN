import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
import network_utils as nut
import utils as ut
import re
import os
from Bunch import Bunch

INPUT = 'input'
FC = 'fully_connected'
CONV = 'convolutional'
POOL = 'max_pooling'
POOL_ARG = 'maxpool_with_args'
DO = 'dropout'
LOSS = 'loss'

activation_voc = {
  's': tf.nn.sigmoid,
  'r': tf.nn.relu,
  't': tf.nn.tanh,
  'i': None
}

CONFIG_COLOR = 30
PADDING = 'SAME'


def clean_unpooling_masks(layer_config):
  """
  Cleans POOL_ARG configs positional information

  :param layer_config: list of layer descriptors
  :return: dictinary of ARGMAX_POOL layer name and corresponding mask source
  """
  mask_list = [cfg.argmax for cfg in layer_config if cfg.type == POOL_ARG]
  for cfg in layer_config:
    if cfg.type == POOL_ARG:
      cfg.argmax = None
  return mask_list


def build_autoencoder(input, layer_config):
  reuse_model = isinstance(layer_config, list)
  if not reuse_model:
    layer_config
    layer_config = layer_config.replace('_', '-').split('-')
    layer_config = [parse_input(input)] + [parse(x) for x in layer_config]
  if not reuse_model:
    ut.print_info('Model config:', color=CONFIG_COLOR)
  enc = build_encoder(input, layer_config, reuse=reuse_model)
  dec = build_decoder(enc, layer_config, reuse=reuse_model)
  mask_list = clean_unpooling_masks(layer_config)
  losses = build_losses(layer_config)
  return Bunch(
    encode=enc,
    decode=dec,
    losses=losses,
    config=layer_config,
    mask_list=mask_list)


def build_encoder(net, layer_config, i=1, reuse=False):
  if i == len(layer_config):
    return net

  cfg = layer_config[i]
  cfg.shape = net.get_shape().as_list()
  name = cfg.enc_op_name if reuse else None
  cfg.ein = net
  if cfg.type == FC:
    if len(cfg.shape) > 2:
      net = slim.flatten(net)
    net = slim.fully_connected(net, cfg.size, activation_fn=cfg.activation,
                               scope=name, reuse=reuse)
  elif cfg.type == CONV:
    net = slim.conv2d(net, cfg.size, [cfg.kernel, cfg.kernel], stride=cfg.stride,
                      activation_fn=cfg.activation, padding=PADDING,
                      scope=name, reuse=reuse)
  elif cfg.type == POOL_ARG:
    net, cfg.argmax = nut.max_pool_with_argmax(net, cfg.kernel)
    # if not reuse:
    #   mask = nut.fake_arg_max_of_max_pool(cfg.shape, cfg.kernel)
    #   cfg.argmax_dummy = tf.constant(mask.flatten(), shape=mask.shape)
  elif cfg.type == POOL:
    net = slim.max_pool2d(net, kernel_size=[cfg.kernel, cfg.kernel], stride=cfg.kernel)
  elif cfg.type == DO:
    net = tf.nn.dropout(net, keep_prob=cfg.keep_prob)
  elif cfg.type == LOSS:
    cfg.arg1 = net
  elif cfg.type == INPUT:
    assert False

  if not reuse:
    cfg.enc_op_name = net.name.split('/')[0]
  if not reuse:
    ut.print_info('\rencoder_%d\t%s\t%s' % (i, str(net), cfg.enc_op_name), color=CONFIG_COLOR)
  return build_encoder(net, layer_config, i + 1, reuse=reuse)


def build_decoder(net, layer_config, i=None, reuse=False, masks=None):
  i = i if i is not None else len(layer_config) - 1

  cfg = layer_config[i]
  name = cfg.dec_op_name if reuse else None
  if len(layer_config) > i + 1:
    if len(layer_config[i + 1].shape) != len(net.get_shape().as_list()):
      net = tf.reshape(net, layer_config[i + 1].shape)

  if i < 0 or layer_config[i].type == INPUT:
    return net

  if cfg.type == FC:
    net = slim.fully_connected(net, int(np.prod(cfg.shape[1:])), scope=name,
                               activation_fn=cfg.activation, reuse=reuse)
  elif cfg.type == CONV:
    net = slim.conv2d_transpose(net, cfg.shape[-1], [cfg.kernel, cfg.kernel], stride=cfg.stride,
                                activation_fn=cfg.activation, padding=PADDING,
                                scope=name, reuse=reuse)
  elif cfg.type == POOL_ARG:
    if cfg.argmax is not None or masks is not None:
      mask = cfg.argmax if cfg.argmax is not None else masks.pop()
      net = nut.unpool(net, mask=mask, stride=cfg.kernel)
    else:
      net = nut.upsample(net, stride=cfg.kernel, mode='COPY')
  elif cfg.type == POOL:
    net = nut.upsample(net, cfg.kernel)
  elif cfg.type == DO:
    pass
  elif cfg.type == LOSS:
    cfg.arg2 = net
  elif cfg.type == INPUT:
    assert False
  if not reuse:
    cfg.dec_op_name = net.name.split('/')[0]
  if not reuse:
    ut.print_info('\rdecoder_%d \t%s' % (i, str(net)), color=CONFIG_COLOR)
  cfg.dout = net
  return build_decoder(net, layer_config, i - 1, reuse=reuse, masks=masks)


def build_stacked_losses(model):
  losses = []
  for i, cfg in enumerate(model.config):
    if cfg.type in [FC, CONV]:
      input = tf.stop_gradient(cfg.ein, name='stacked_breakpoint_%d' % i)
      net = build_encoder(input, [None, model.config[i]], reuse=True)
      net = build_decoder(net, [model.config[i]], reuse=True)
      losses.append(l2_loss(input, net, name='stacked_loss_%d' % i))
  model.stacked_losses = losses


def build_losses(layer_config):
  return []


def l2_loss(arg1, arg2, alpha=1.0, name='reco_loss'):
  with tf.name_scope(name):
    loss = tf.nn.l2_loss(arg1 - arg2)
    return alpha * loss


def get_activation(descriptor):
  if 'c' not in descriptor and 'f' not in descriptor:
    return None
  activation = tf.nn.relu if 'c' in descriptor else tf.nn.sigmoid
  act_descriptor = re.search('[r|s|i|t]&', descriptor)
  if act_descriptor is None:
    return activation
  act_descriptor = act_descriptor.group(0)
  return activation_voc[act_descriptor]


def _get_cfg_dummy():
  return Bunch(enc_op_name=None, dec_op_name=None)


def parse(descriptor):
  item = _get_cfg_dummy()

  match = re.match(r'^((\d+c\d+(s\d+)?[r|s|i|t]?)'
                   r'|(f\d+[r|s|i|t]?)'
                   r'|(d0?\.?[\d+]?)'
                   r'|(d0?\.?[\d+]?)'
                   r'|(p\d+)'
                   r'|(ap\d+))$', descriptor)
  assert match is not None, 'Check your writing: %s (f10i-3c64r-d0.1-p2-ap2)' % descriptor


  if 'f' in descriptor:
    item.type = FC
    item.activation = get_activation(descriptor)
    item.size = int(re.search('f\d+', descriptor).group(0)[1:])
  elif 'c' in descriptor:
    item.type = CONV
    item.activation = get_activation(descriptor)
    item.kernel = int(re.search('c\d+', descriptor).group(0)[1:])
    stride = re.search('s\d+', descriptor)
    item.stride = int(stride.group(0)[1:]) if stride is not None else 1
    item.size = int(re.search('\d+c', descriptor).group(0)[:-1])
  elif 'd' in descriptor:
    item.type = DO
    item.keep_prob = float(descriptor[1:])
  elif 'ap' in descriptor:
    item.type = POOL_ARG
    item.kernel = int(descriptor[2:])
  elif 'p' in descriptor:
    item.type = POOL
    item.kernel = int(descriptor[1:])
  elif 'l' in descriptor:
    item.type = LOSS
    item.loss_type = 'l2'
    item.alpha = float(descriptor.split('l')[0])
  else:
    print('What is "%s"? Check your writing 16c2i-7c3r-p3-0.01l-f10t-d0.3' % descriptor)
    assert False
  return item


def parse_input(input):
  item = _get_cfg_dummy()
  item.type = INPUT
  item.shape = input.get_shape().as_list()
  item.dout = input
  return item


def _log_graph():
  path = '/tmp/interpreter'
  with tf.Session() as sess:
    tf.global_variables_initializer()
    tf.summary.FileWriter(path, sess.graph)
    ut.print_color(os.path.abspath(path), color=33)