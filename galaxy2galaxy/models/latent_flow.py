"""Normalizing flow models learning the latent space of an existing Auto-Encoder
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import numpy as np

from tensor2tensor.layers import common_attention
from tensor2tensor.layers import common_hparams
from tensor2tensor.layers import common_layers
from tensor2tensor.layers import modalities
from tensor2tensor.utils import registry
from tensor2tensor.utils import t2t_model

from galaxy2galaxy.layers.flows import masked_autoregressive_conditional_template

import tensorflow as tf
import tensorflow_hub as hub
import tensorflow_probability as tfp
tfb = tfp.bijectors
tfd = tfp.distributions


class LatentFlow(t2t_model.T2TModel):
  """ Base class for latent flows

  This assumes that an already exported tensorflow hub autoencoder is provided
  in hparams.
  """

  def normalizing_flow(self, condition):
    """ Function building a normalizing flow, returned as a Tensorflow probability
    distribution
    """
    raise NotImplementedError

  def infer(self,
            features=None,
            decode_length=50,
            beam_size=1,
            top_beams=1,
            alpha=0.0,
            use_tpu=False):
    """ TODO: Switch to parent inference function
    """
    return self(features)[0]

  def body(self, features):
    hparams = self.hparams
    is_training = (hparams.mode == tf.estimator.ModeKeys.TRAIN)

    x = features['inputs']
    hparamsp = hparams.problem.get_hparams()
    y = tf.concat([tf.expand_dims(features[k], axis=1) for k in hparamsp.attributes] ,axis=1)

    # Load the encoder and decoder modules
    encoder = hub.Module(hparams.encoder_module, trainable=False)

    if hparams.mode == tf.estimator.ModeKeys.PREDICT:
      def flow_module_spec():
        inputs = {k: tf.placeholder(tf.float32, shape=[None]) for k in hparamsp.attributes}
        y = tf.concat([tf.expand_dims(inputs[k], axis=1) for k in inputs.keys()],axis=1)
        y = common_layers.layer_norm(y, name="y_norm")
        flow = self.normalizing_flow(y)
        hub.add_signature(inputs=inputs, outputs=flow.sample(tf.shape(y)[0]))
      flow_spec = hub.create_module_spec(flow_module_spec)
      flow = hub.Module(flow_spec, name='flow_module')
      hub.register_module_for_export(flow, "code_sampler")
      code_sample = flow({k: features[k] for k in hparamsp.attributes})
      return code_sample, {'loglikelihood': 0}

    # Encode the input image
    if hparams.encode_psf and 'psf' in features:
      code = encoder({'input':x, 'psf': features['psf']})
    else:
      code = encoder(x)

    with tf.variable_scope("flow_module"):
      # Apply some amount of normalization to the features
      y = common_layers.layer_norm(y, name="y_norm")
      flow = self.normalizing_flow(y)
      samples = flow.sample(tf.shape(y)[0])
      loglikelihood = flow.log_prob(tf.layers.flatten(code))

    # This is the loglikelihood of a batch of images
    tf.summary.scalar('loglikelihood', tf.reduce_mean(loglikelihood))
    loss = - tf.reduce_mean(loglikelihood)

    return samples, {'training': loss}


@registry.register_model
class LatentMAF(LatentFlow):

  def normalizing_flow(self, conditioning):
    """
    Normalizing flow based on Masked AutoRegressive Model.
    """
    hparams = self.hparams
    latent_size = hparams.latent_size

    def init_once(x, name, trainable=False):
      return tf.get_variable(name, initializer=x, trainable=trainable)

    chain = []
    for i in range(hparams.num_hidden_layers):
      chain.append(tfb.MaskedAutoregressiveFlow(
                  shift_and_log_scale_fn=masked_autoregressive_conditional_template(
                  hidden_layers=[hparams.hidden_size, hparams.hidden_size],
                      conditional_tensor=conditioning,
                      activation=common_layers.belu, name='maf%d'%i)))
      chain.append(tfb.Permute(permutation=init_once(
                           np.random.permutation(latent_size).astype("int32"),
                           name='permutation%d'%i)))
    chain = tfb.Chain(chain)

    flow = tfd.TransformedDistribution(distribution=tfd.MultivariateNormalDiag(loc=np.zeros(latent_size, dtype='float32'),
                                                                               scale_diag=np.ones(latent_size, dtype='float32')),
            bijector=chain)
    return flow


@registry.register_hparams
def latent_flow():
  """Basic autoencoder model."""
  hparams = common_hparams.basic_params1()
  hparams.optimizer = "adam"
  hparams.learning_rate_constant = 0.1
  hparams.learning_rate_warmup_steps = 100
  hparams.learning_rate_schedule = "constant * linear_warmup * rsqrt_decay"
  hparams.label_smoothing = 0.0
  hparams.batch_size = 128
  hparams.hidden_size = 512
  hparams.num_hidden_layers = 4
  hparams.initializer = "uniform_unit_scaling"
  hparams.initializer_gain = 1.0
  hparams.weight_decay = 0.0
  hparams.kernel_height = 4
  hparams.kernel_width = 4
  hparams.dropout = 0.05

  hparams.add_hparam("latent_size", 64)

  # hparams specifying the encoder
  hparams.add_hparam("encoder_module", "") # This needs to be overriden

  # hparams related to the PSF
  hparams.add_hparam("encode_psf", True) # Should we use the PSF at the encoder

  return hparams