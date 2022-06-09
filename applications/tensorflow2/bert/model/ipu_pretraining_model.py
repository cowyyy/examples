# Copyright (c) 2021 Graphcore Ltd. All rights reserved.
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This file has been modified by Graphcore Ltd.
import tensorflow as tf
from tensorflow.python import ipu
from transformers.models.bert.configuration_bert import BertConfig
from transformers.models.bert.modeling_tf_bert import (
    input_processing,
    TFBertForPreTraining,
    TFBertForPreTrainingOutput
)


def gather_positions(inputs, positions):
    """
    Given an input tensor of size (micro batch size, sequence length, width) and a positions tensor of shape
    (batch_size, max_number_masked_elements), this method gathers the input values at the given positions for each
    batch.
    :param inputs: Last hidden state of the encoder.
    :param positions: Positions of the masked MLM tokens.
    :return: Inputs at the given positions, with shape (batch_size, max_number_masked_elements).
    """
    micro_batch_size, seq_length, hidden_size = inputs.shape
    _, num_masked_tokens = positions.shape

    flat_offsets = tf.reshape(tf.range(0, micro_batch_size, dtype=tf.int32) * seq_length, (-1, 1))
    flat_positions = tf.reshape(positions + flat_offsets, (-1,))
    flat_sequence_tensor = tf.reshape(inputs, (micro_batch_size * seq_length, hidden_size))
    output_tensor = ipu.ops.embedding_ops.embedding_lookup(
        flat_sequence_tensor, flat_positions, serialization_factor=1)
    output_tensor = tf.reshape(output_tensor, (micro_batch_size, num_masked_tokens, hidden_size))
    return output_tensor


class GatherSubsetOutput(tf.keras.layers.Layer):
    def __init__(self, name='gather_masked_outputs', **kwargs):
        super().__init__(trainable=False, name=name, **kwargs)

    def call(self, inputs, positions, **kwargs):
        return gather_positions(inputs, positions)

class SPMD(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()

    def call(self, x: tf.Tensor):
        import numpy as np
        from tensorflow.compiler.xla.experimental.xla_sharding import xla_sharding
        input_mesh_shape = [4] + [1] * (len(x.shape) - 1)
        x = xla_sharding.tile(x,
                              np.arange(4).reshape(input_mesh_shape))

        return x

class IpuTFBertForPreTraining(TFBertForPreTraining):
    """
    Extends class TFBertForPreTraining to wrap its call method. The original call passes the complete hidden state
    that is output of the encoder to the MLM head. Here we post-process the output of the encoder by taking only the
    positions that correspond to masked tokens, so that we can pass a smaller tensor to the MLM head. This results
    in a more memory efficient implementation.
    """

    def __init__(self, config: BertConfig, *inputs, **kwargs):
        super().__init__(config, *inputs, **kwargs)
        self.gather_masked = GatherSubsetOutput(name='gather_masked_outputs')
        self.spmd = SPMD()

    def call(
            self,
            input_ids=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            labels=None,
            next_sentence_label=None,
            training=False,
            **kwargs,
    ):
        inputs = input_processing(
            func=self.call,
            config=self.config,
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            labels=labels,
            next_sentence_label=next_sentence_label,
            training=training,
            kwargs_call=kwargs,
        )
        inputs["inputs_embeds"] = self.spmd(inputs["inputs_embeds"])
        outputs = self.bert(
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            token_type_ids=inputs["token_type_ids"],
            position_ids=inputs["position_ids"],
            head_mask=inputs["head_mask"],
            inputs_embeds=inputs["inputs_embeds"],
            output_attentions=inputs["output_attentions"],
            output_hidden_states=inputs["output_hidden_states"],
            return_dict=inputs["return_dict"],
            training=inputs["training"],
        )
        sequence_output, pooled_output = outputs[:2]

        # Post-process sequence_output to get only the elements corresponding with the masked tokens.
        sequence_output = self.gather_masked(sequence_output, inputs['masked_lm_positions'])

        prediction_scores = self.mlm(sequence_output=sequence_output, training=inputs["training"])
        seq_relationship_score = self.nsp(pooled_output=pooled_output)
        total_loss = None

        if inputs["labels"] is not None and inputs["next_sentence_label"] is not None:
            d_labels = {"labels": inputs["labels"], "next_sentence_label": inputs["next_sentence_label"]}
            total_loss = self.compute_loss(labels=d_labels, logits=(prediction_scores, seq_relationship_score))

        if not inputs["return_dict"]:
            output = (prediction_scores, seq_relationship_score) + outputs[2:]
            return ((total_loss,) + output) if total_loss is not None else output

        return TFBertForPreTrainingOutput(
            loss=total_loss,
            prediction_logits=prediction_scores,
            seq_relationship_logits=seq_relationship_score,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
