# Copyright (c) 2021 Graphcore Ltd. All rights reserved.

"""Layer-wise Adaptive Rate Scaling (LARS) optimizer."""

import re

import tensorflow as tf
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import linalg_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.training import training_ops

from ipu_tensorflow_addons.keras.optimizers import IpuOptimizerBase


class LARSIpuOptimizer(IpuOptimizerBase):
    """Layer-wise Adaptive Rate Scaling for large batch training.
    Introduced by "Large Batch Training of Convolutional Networks" by Y. You,
    I. Gitman, and B. Ginsburg. (https://arxiv.org/abs/1708.03888)
    Implements the LARS learning rate scheme presented in the paper above. This
    optimizer is useful when scaling the batch size to up to 32K without
    significant performance degradation. It is recommended to use the optimizer
    in conjunction with:
        - Gradual learning rate warm-up
        - Linear learning rate scaling
        - Poly rule learning rate decay
    Note, LARS scaling is currently only enabled for dense tensors. Update
    for Sparse tensors is not implemented."""

    def __init__(
        self,
        learning_rate=0.001,
        momentum=0.9,
        weight_decay=0.0001,
        eeta=0.001,
        epsilon=0.0,
        name='LARSOptimizer',
        exclude_from_weight_decay=None,
        exclude_from_layer_adaptation=None,
        m_dtype=None,
        optimizer_compute_precisions=(dtypes.float32, dtypes.float32),
        use_nesterov=False,
        **kwargs
    ):
        """
        Args:
          learning_rate: A `Tensor` or a floating point value. or a schedule
            that is a `tf.keras.optimizers.schedules.LearningRateSchedule`
            The learning rate.
          momentum: A `float` value or a constant `float` tensor.
            The exponential decay rate for the 1st moment estimates.
          weight_decay_rate: weight decay rate.
          eeta: LARS coefficient as used in the paper. Default set to LARS
                coefficient from the paper. (eeta / weight_decay) determines
                the highest scaling factor in LARS.
          epsilon: Optional epsilon parameter to be set in models that have very
                   small gradients. Default set to 0.0.
          exclude_from_weight_decay: List of regex patterns of
            variables excluded from weight decay. Variables whose name
            contain a substring matching the pattern will be excluded.
          exclude_from_layer_adaptation: List of regex patterns of
            variables excluded from layer adaptation. Variables whose name
            contain a substring matching the pattern will be excluded.
          name: Optional name for the operationsśś created when applying
            gradients. Defaults to "LARSOptimizer".
          m_dtype: Dtype of the optimizer state m. If None, will set to
            dtypes of the vars.
          optimizer_compute_precisions: Tuple of TF dtypes that determine
            what precision the stages of optimizer compute are done in.
            This optimizer has two stages of compute precision so the
            tuple must be of size 2.
          use_nesterov: when set to True, nesterov momentum will be enabled
          **kwargs: keyword arguments. Allowed to be {`clipnorm`,
            `clipvalue`, `lr`, `decay`}. `clipnorm` is clip gradients by
            norm; `clipvalue` is clip gradients by value, `decay` is
            included for backward compatibility to allow time inverse
            decay of learning rate. `lr` is included for backward
            compatibility, recommended to use `learning_rate` instead.
        """
        super().__init__(name, **kwargs)
        self._set_hyper("eeta", eeta)
        self._set_hyper("weight_decay", weight_decay)
        self._set_hyper("learning_rate", kwargs.get("lr", learning_rate))
        self._set_hyper("decay", self._initial_decay)
        self._set_hyper("momentum", momentum)
        self.epsilon = epsilon or tf.keras.backend.epsilon()
        self.exclude_from_weight_decay = exclude_from_weight_decay
        self.exclude_from_layer_adaptation = exclude_from_layer_adaptation
        self.use_nesterov = use_nesterov
        self.m_dtype = m_dtype

        self.opt_dtypes = optimizer_compute_precisions
        if len(self.opt_dtypes) != 2:
            raise ValueError(
                "Must provide a list of two elements for the optimizer"
                " compute precision. The final stage of the weight update"
                " can be done in a different precision to the initial stage.")

    def _create_slots(self, var_list):
        for var in var_list:
            self.add_slot_with_dtype(var, "momentum_var", self.m_dtype)

    def _prepare_local(self, var_device, var_dtype, apply_state):
        super()._prepare_local(var_device, var_dtype, apply_state)
        compute_dtype = self.opt_dtypes[0]
        eeta = array_ops.identity(self._get_hyper("eeta", compute_dtype))
        momentum = array_ops.identity(self._get_hyper("momentum", compute_dtype))
        weight_decay = array_ops.identity(
            self._get_hyper("weight_decay", compute_dtype))
        apply_state[(var_device, var_dtype)].update(
            dict(
                eeta=eeta,
                weight_decay=weight_decay,
                epsilon=ops.convert_to_tensor(self.epsilon, compute_dtype),
                momentum=momentum,
            )
        )

    def update_coefficients(self, handle, apply_state):
        var_device, var_dtype = handle.device, handle.dtype.base_dtype
        # update coefficients
        return ((apply_state or {}).get((var_device, var_dtype)) or
                self._fallback_apply_state(var_device, var_dtype))

    def compute_trust_ratio(self,
                            grad,
                            var,
                            eeta,
                            epsilon,
                            weight_decay,
                            compute_dtype):

        var_name = self._get_variable_name(var.name)

        if self._do_layer_adaptation(var_name):
            w_norm = linalg_ops.norm(var, ord=2)
            g_norm = linalg_ops.norm(grad, ord=2)

            w_norm_cast = math_ops.cast(w_norm, dtype=compute_dtype)
            g_norm_cast = math_ops.cast(g_norm, dtype=compute_dtype)

            if self._do_use_weight_decay(var_name):
                grad += (math_ops.cast(weight_decay, dtype=grad.dtype) *
                         math_ops.cast(var, dtype=grad.dtype))
            else:
                weight_decay = 0

            trust_ratio = array_ops.where(
                condition=math_ops.greater(w_norm_cast, 0),
                x=array_ops.where(
                    condition=math_ops.greater(g_norm_cast, 0),
                    x=eeta * w_norm_cast / (g_norm_cast + weight_decay * w_norm_cast + epsilon),
                    y=constant_op.constant(1.0, dtype=compute_dtype, shape=w_norm.shape)),
                y=constant_op.constant(1.0, dtype=compute_dtype, shape=w_norm.shape))
        else:
            trust_ratio = constant_op.constant(1.0, dtype=compute_dtype)

        return math_ops.cast(trust_ratio, grad.dtype), grad

    def _resource_apply_dense(self, grad, var, apply_state=None):
        coefficients = self.update_coefficients(var, apply_state)
        eeta = coefficients['eeta']
        lr_t = coefficients['lr_t']
        epsilon = coefficients['epsilon']
        momentum = coefficients['momentum']
        weight_decay = coefficients['weight_decay']

        momentum_var = self.get_slot(var, 'momentum_var')

        compute_dtype = self.opt_dtypes[0]

        trust_ratio, grad = self.compute_trust_ratio(
            grad, var, eeta, epsilon, weight_decay, compute_dtype)
        scaled_lr = lr_t * trust_ratio

        return training_ops.resource_apply_momentum(
            var=var.handle,
            accum=momentum_var.handle,
            lr=math_ops.cast(1.0, var.dtype.base_dtype),
            grad=math_ops.cast(grad * scaled_lr, var.dtype.base_dtype),
            momentum=math_ops.cast(momentum, var.dtype.base_dtype),
            use_locking=False,
            use_nesterov=self.use_nesterov
        )

    def get_config(self):
        config = super().get_config()
        config.update({
            "learning_rate": self._serialize_hyperparameter("learning_rate"),
            "weight_decay": self._serialize_hyperparameter("weight_decay"),
            "decay": self._serialize_hyperparameter("decay"),
            "momentum": self._serialize_hyperparameter("momentum"),
            "eeta": self._serialize_hyperparameter("eeta"),
            "epsilon": self.epsilon,
            'm_dtype': self.m_dtype
        })
        return config

    def _do_use_weight_decay(self, param_name):
        """Whether to use L2 weight decay for `param_name`."""
        if self.exclude_from_weight_decay:
            for r in self.exclude_from_weight_decay:
                if re.search(r, param_name) is not None:
                    return False
        return True

    def _do_layer_adaptation(self, param_name):
        """Whether to do layer-wise learning rate adaptation for
        `param_name`."""
        if self.exclude_from_layer_adaptation:
            for r in self.exclude_from_layer_adaptation:
                if re.search(r, param_name) is not None:
                    return False
        return True

    def _get_variable_name(self, param_name):
        """Get the variable name from the tensor name."""
        m = re.match("^(.*):\\d+$", param_name)
        if m is not None:
            param_name = m.group(1)
        return param_name
