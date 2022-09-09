
#  Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
# 
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#      * Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#      * Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#      * Neither the name of the NVIDIA CORPORATION nor the
#        names of its contributors may be used to endorse or promote products
#        derived from this software without specific prior written permission.
# 
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
#  ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL NVIDIA CORPORATION BE LIABLE FOR ANY
#  DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#  (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#  LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#  ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
# 
# *****************************************************************************

"""
wavenet.py forked from nv-wavenet/pytorch:
https://github.com/NVIDIA/nv-wavenet/blob/master/pytorch/wavenet.py

Modified by Gary Plunkett, Jan 2019
Added fast inference in convolution module, inference methods, and lots
of config options.
"""

import torch
import math
import random
import time
from collections import deque
from . import debug

import numpy as np
import torch.nn.functional as F
import utils
from nn.discretized_mix_logistics import SampleDiscretizedMixLogistics


class Conv(torch.nn.Module):
    """
    A convolution with the option to be causal and use xavier initialization
    Implements "Fast Wavenet Generation Algorithm" for quick inference: 
                                       https://arxiv.org/abs/1611.09482
    """
    def __init__(self, in_channels, out_channels, kernel_size=1, stride=1,
                 dilation=1, bias=True, w_init_gain='linear', is_causal=False,
                 use_act=False):
        super(Conv, self).__init__()
        self.is_causal = is_causal
        self.kernel_size = kernel_size
        self.in_channels = in_channels
        self.dilation = dilation

        # Input memory activates if infer_sample() called
        self.input_memory = None
        
        self.conv = torch.nn.Conv1d(in_channels, out_channels,
                                    kernel_size=kernel_size, stride=stride,
                                    dilation=dilation, bias=bias)

        # Softsign activation recommended by DeepVoice3, a tanh alternative
        self.use_act = use_act
        if self.use_act:
            self.act = torch.nn.Softsign()
        
        torch.nn.init.xavier_uniform_(
            self.conv.weight, gain=torch.nn.init.calculate_gain(w_init_gain))

    def forward(self, signal):
        if self.is_causal:
            padding = (int((self.kernel_size - 1) * (self.dilation)), 0)
            signal = torch.nn.functional.pad(signal, padding).cuda()
        signal = self.conv(signal)
        if self.use_act:
            signal = self.act(signal)
        return signal
    
    def infer_step(self, x):
        """
        Added by Gary Plunkett, Feb 2019
        - Works for any dilation size
        - Only tested with stride of 1
        - Only works with kernels of size of 1 or 2
        - If kernel_size==1, infer_step equivalent to foward
        - Don't call this function if kernel size > 1 and non-causal
        """
        
        # add singleton batch dimension
        if (len(x.size()) <= 2):
            x = x.unsqueeze(-1)

        # use last time sample if handed a sequence
        if (x.size(-1) > 1):
            x = x[:, :, -1]

        if self.kernel_size == 1:
            return self.conv(x)

        elif self.is_causal:
            if self.input_memory is None:
                self.init_input_memory(x)
                
            self.input_memory.appendleft(x.clone())
            x0 = self.input_memory.pop()
            x0_x1 = torch.cat((x0, x), 2)
            W = self.conv.weight.data

            if self.conv.bias is None:
                return F.conv1d(x0_x1, W)

            B = self.conv.bias.data
            
            return F.conv1d(x0_x1, W, B)

    def init_input_memory(self, x):
        # Initialize memory queue and fill w/ zero vectors
        # Each zero is a new view on same underlying storage (mem efficiency)
        self.input_memory = deque()
        device = x.device
        zero_sample = torch.zeros(size=[x.size(0), self.in_channels, 1]).to(device)
        for i in range(self.dilation):
            self.input_memory.append(zero_sample.view(zero_sample.size()))
        

class UpsampleByRepetition(torch.nn.Module):
    """
    Upsample by repitition expects a (B x C x T) tensor
    Returns a (B x C x (T*upscale)) tensor
    Doesn't duplicate underlying storage
    """
    
    def __init__(self, upscale):
        super(UpsampleByRepetition, self).__init__()
        self.upscale = upscale
    
    def forward(self, X):
        upsamp = X.unsqueeze(2)
        upsamp = upsamp.expand(X.size(0), X.size(1), self.upscale, X.size(2))
        upsamp = upsamp.transpose(2, 3).flatten(2, 3)
        assert(upsamp.size()[:-1] == X.size()[:-1])
        assert(upsamp.size(2) == self.upscale * X.size(2))
        return upsamp


class QuantizedInputLayer(torch.nn.Module):
    """
    Learns an embedding for quantized values (256 mu-qunatized audio)
    Optionally applies a softsign activation
    """
    
    def __init__(self, n_in_channels, n_out_channels, use_act):
        super(QuantizedInputLayer, self).__init__()
        self.embed = torch.nn.Embedding(n_in_channels, n_out_channels)
        self.act = torch.nn.Softsign()
        self.use_act = use_act

    def forward(self, x):
        x = self.embed(x)
        x.transpose_(1, 2)
        if self.use_act:
            x = self.act(x)
        return x

    
class Wavenet(torch.nn.Module):
    def __init__(self, onehot_input, n_in_channels, use_in_bias, use_in_act,
                 n_layers, max_dilation, n_residual_channels, use_dilate_bias, use_res_out_conv, use_res_out_bias, res_block_gain,
                 use_skip_out, n_skip_channels, use_skip_bias, n_skip_to_out_channels, n_out_channels,
                 use_conditioning, same_cond_each_resblock, n_cond_channels, use_cond_conv, use_cond_bias, use_cond_act,
                 resblock_drop_prob, out_drop_prob,
                 upsamp_scale, upsample_by_copy, upsamp_conv_window, name):
        super(Wavenet, self).__init__()

        self.name = name
        
        self.n_layers = n_layers
        self.max_dilation = max_dilation
        self.n_residual_channels = n_residual_channels 
        self.n_out_channels = n_out_channels
        self.upscale = upsamp_scale
        self.downscale = 1./upsamp_scale        

        self.use_conditioning = use_conditioning
        self.same_cond_each_resblock = same_cond_each_resblock
        self.use_cond_conv = use_cond_conv        
        self.use_cond_bias = use_cond_bias

        self.use_skip_out = use_skip_out
        
        self.use_dilate_bias = use_dilate_bias
        self.use_skip_bias = use_skip_bias
        self.use_res_out_bias = use_res_out_bias
        
        self.use_res_out_conv = use_res_out_conv
        self.res_block_gain = res_block_gain
        
        if upsample_by_copy:
            self.upsample = UpsampleByRepetition(self.upscale)      
        else:
            self.upsample = torch.nn.ConvTranspose1d(n_cond_channels,
                                                     n_cond_channels,
                                                     upsamp_conv_window,
                                                     self.upscale)
        # conditioning layer
        if self.use_cond_conv:
            if self.same_cond_each_resblock:
                cond_out_ch = 2*n_residual_channels
            else:
                cond_out_ch = 2*n_residual_channels*n_layers
            self.cond_layers = Conv(n_cond_channels, cond_out_ch,
                                    w_init_gain='tanh', use_act=use_cond_act,
                                    bias=self.use_cond_bias)

        self.dilate_layers = torch.nn.ModuleList()
        self.res_layers = torch.nn.ModuleList()
        if self.use_skip_out:
            self.skip_layers = torch.nn.ModuleList()

        # input layer 
        if onehot_input:
            self.in_layer = QuantizedInputLayer(n_in_channels, n_residual_channels,
                                                use_in_act)
        else:
            self.in_layer = Conv(n_in_channels, n_residual_channels,
                                 w_init_gain='tanh', bias=use_in_bias,
                                 use_act=use_in_act)

        # skip and output layers
        if self.use_skip_out:
            self.conv_out = Conv(n_skip_channels, n_skip_to_out_channels,
                                 bias=False, w_init_gain='relu')
            self.conv_end = Conv(n_skip_to_out_channels, n_out_channels,
                                 bias=False, w_init_gain='linear')
            self.out_dropout = torch.nn.Dropout(p=out_drop_prob)
            
        # res blocks
        loop_factor = math.floor(math.log2(max_dilation)) + 1
        for i in range(n_layers):
            dilation = 2 ** (i % loop_factor)
            in_layer = Conv(n_residual_channels, 2*n_residual_channels,
                            kernel_size=2, dilation=dilation,
                            w_init_gain='tanh', is_causal=True,
                            bias=self.use_dilate_bias)
            self.dilate_layers.append(in_layer)

            # last one is not necessary
            if (self.use_res_out_conv) and (i < n_layers - 1):
                res_layer = Conv(n_residual_channels, n_residual_channels,
                                 w_init_gain='linear', bias=self.use_res_out_bias)
                self.res_layers.append(res_layer)

            if self.use_skip_out:
                skip_layer = Conv(n_residual_channels, n_skip_channels,
                                  w_init_gain='relu', bias=self.use_skip_bias)
                self.skip_layers.append(skip_layer)
                
        self.resblock_dropout = torch.nn.Dropout(p=resblock_drop_prob)
            
            
    def forward(self, forward_input, training=True):

        features = forward_input[0]
        forward_input = forward_input[1]

        if self.use_conditioning:
            if (self.upscale != 1):
                cond_input = self.upsample(features)
            else:
                cond_input = features
            assert(cond_input.size(2) >= forward_input.size(-1))
            if cond_input.size(2) > forward_input.size(-1):
                cond_input = cond_input[:, :, :forward_input.size(-1)]
            if self.use_cond_conv:
                cond_input = self.cond_layers(cond_input)
                # debug.plot_tensor(cond_input, "test/"  + self.name + "/cond_input")
            if not self.same_cond_each_resblock:
                cond_input = cond_input.view(cond_input.size(0), self.n_layers, -1, cond_input.size(2))

        # debug.plot_tensor(forward_input, "test/" + self.name + "/raw_input")
        
        forward_input = self.in_layer(forward_input)

        # debug.plot_tensor(forward_input, "test/" + self.name + "/input_transform")

        # Residual block loop
        for i in range(self.n_layers):
            if training:
                forward_input = self.resblock_dropout(forward_input)

            in_act = self.dilate_layers[i](forward_input)

            if self.use_conditioning:
                if self.same_cond_each_resblock:
                    cond_act = cond_input
                else:
                    cond_act = cond_input[:, i, :, :]
                in_act = in_act + cond_act                    
            
            t_act = F.tanh(in_act[:, :self.n_residual_channels, :])
            s_act = F.sigmoid(in_act[:, self.n_residual_channels:, :])
            acts = t_act * s_act

            # debug.plot_tensor(acts, "test/" + self.name + "/act" + str(i))

            if self.use_skip_out:
                if i == 0:
                    output = self.skip_layers[i](acts)
                else:
                    output = self.skip_layers[i](acts) + output

            if (self.use_res_out_conv) and (i < len(self.res_layers)):
                acts = self.res_layers[i](acts)

            forward_input = acts + forward_input
            forward_input = forward_input * self.res_block_gain #from DeepVoice3, reduce input variance early in training


        # Feed forward output if using skip out
        if self.use_skip_out:

            # debug.plot_tensor(output, "test/" + self.name + "/skip_out")
            
            # Fully-connected output
            if training:
                output = self.out_dropout(output)
            output = torch.nn.functional.relu(output, True)
            output = self.conv_out(output)
            if training:
                output = self.out_dropout(output)
            output = torch.nn.functional.relu(output, True)
            output = self.conv_end(output)

        else:
            output = forward_input
            
        # Remove last probabilities because they've seen all the data
        last = output[:, :, -1]
        last = last.unsqueeze(2)
        output = output[:, :, :-1]

        # Replace probability for first value with 0's because we don't know
        first = last * 0.0
        output = torch.cat((first, output), dim=2)

        # debug.plot_tensor(output, "test/" + self.name + "/final_out")
        
        return output


    def infer_step(self, cond_input, forward_input):
        """
        cond_input: B x n_layer x C
        forward_input: T ints
        """

        # Add singleton time dimension
        # FLAG when I add batching, check size before adding batch dim
        forward_input = forward_input.unsqueeze(-1)
        forward_input = self.in_layer(forward_input)

        if self.use_conditioning:
            cond_input = cond_input.unsqueeze(-1)
        
        # Residual block loop
        for i in range(self.n_layers):

            in_act = self.dilate_layers[i].infer_step(forward_input)

            if self.use_conditioning:
                if self.use_cond_conv:
                    cond_act = cond_input[:, i, :, :]
                else:
                    cond_act = cond_input
                in_act = in_act + cond_act
                
            t_act = F.tanh(in_act[:, :self.n_residual_channels, :])
            s_act = F.sigmoid(in_act[:, self.n_residual_channels:, :])
            acts = t_act * s_act

            if self.use_skip_out:
                if i == 0:
                    output = self.skip_layers[i](acts)
                else:
                    output = self.skip_layers[i](acts) + output

            if (self.use_res_out_conv) and (i < len(self.res_layers)):
                acts = self.res_layers[i](acts)

            forward_input = acts + forward_input
            forward_input = forward_input * self.res_block_gain
            
        if self.use_skip_out:
            output = torch.nn.functional.relu(output, True)
            output = self.conv_out(output)
            output = torch.nn.functional.relu(output, True)
            output = self.conv_end(output).squeeze()
        else:
            output = forward_input
            
        return output

    
    def inference(self, cond_features, use_logistic_mix = False,
                  teacher_audio=None, mu_quantization=256,
                  randomize_input=False, rand_sample_chance=0.,
                  length=0, audio_hz=16000, batch_size=0,
                  cond_channels=0, device="cuda"): 
        """
        Generates audio samples equivalent to the length of upsampled cond features
        - Will use teacher audio as forward input, if provided
        - If teacher_audio_length < features_length, switches forward input to inference 
              samples when teacher samples exhasted.
        - If cond_features=None, generates unconditional output. Last four params 
              (length, batch_size, cond_channels, device) control unconditional output.
        """

        
        if self.use_conditioning:
            assert((cond_features is not None) or (length > 0))
            
            # get metadata from condition features
            if cond_features is not None:
                assert(len(cond_features.size()) == 3)

                device = cond_features.device
                length = cond_features.size(-1) * self.upscale
                cond_channels = cond_features.size(1)
                batch_size = cond_features.size(0)

                if (self.upscale != 1):
                    cond_features = self.upsample(cond_features)

            else:
                assert(batch_size > 0 and cond_channels > 0)
                cond_features = torch.zeros(size=[batch_size, cond_channels, length]).to(device)

            if self.use_cond_conv:
                # make condition features for every timestep and res layer
                cond_features = self.cond_layers(cond_features)
            if not self.same_cond_each_resblock:
                cond_features = cond_features.view(batch_size, self.n_layers, 2*self.n_residual_channels, length)

        else: # no condtioning
            length = length * audio_hz
        
        # output buffers
        logits = torch.zeros(self.n_out_channels, length).to(device)
        output_audio = torch.zeros(size=[batch_size, length+1]).to(device)
        output_audio = utils.mu_law_encode(output_audio)
        
        if teacher_audio is not None:
            teacher_length = teacher_audio.size(1)
        else:
            teacher_length = 0

        if use_logistic_mix:
            sampler = SampleDiscretizedMixLogistics()
        else:
            sampler = utils.CategoricalSampler()

        #################
        # inference loop:
        ##################
        start_time = time.time()
        print("Inference progress:")
        for s in range(length-1):

            # print progress every 100 samples
            if (s%100 == 0):
                print(str(s / length), end='\r', flush=True)

            if self.use_conditioning:
                if self.use_cond_conv:
                    cond_sample = cond_features[:, :, :, s]
                else:
                    cond_sample = cond_features[:, :, s]
            else:
                cond_sample = None
                    
            # flip biased coin to see if raandom sample used
            if randomize_input and (random.uniform < rand_sample_chance):
                    forward_sample = torch.randint_like(forward_sample,
                                                        low=0, high=mu_quantization)
            else:
                # draw from teacher or previous sample?
                if (s < teacher_length):
                    forward_sample = teacher_audio[:, s].clone()
                else:
                    forward_sample = output_audio[:, s].clone()

            logits[:, s+1] = self.infer_step(cond_sample, forward_sample)
            
            output_audio[:, s+1] = sampler(logits[:, s+1])

        end_time = time.time()
        ###################
        # end inference
        ###################

        print("Inference complete in " + str(end_time - start_time))
            
        return utils.mu_law_decode(output_audio, mu_quantization)

    
    def cond_input_for_inference(cond_features, ):
        """
        Helper method for preprocessing cond features for inference
        """
        
        assert((cond_features is not None) or (length > 0))
        
        # get metadata from condition features
        if cond_features is not None:
            assert(len(cond_features.size()) == 3)

            device = cond_features.device
            length = cond_features.size(-1) * self.upscale
            cond_channels = cond_features.size(1)
            batch_size = cond_features.size(0)

            if (self.upscale != 1):
                cond_features = self.upsample(cond_features)

        else:
            assert(batch_size > 0 and cond_channels > 0)
            cond_features = torch.zeros(size=[batch_size, cond_channels, length]).to(device)

        if self.use_cond_conv:
            # make condition features for every timestep and res layer
            cond_features = self.cond_layers(cond_features)
        if not self.same_cond_each_resblock:
            cond_features = cond_features.view(batch_size, self.n_layers, 2*self.n_residual_channels, length)

        return cond_features

    
    def export_weights(self):
        """
        Returns a dictionary with tensors ready for nv_wavenet wrapper
        """
        model = {}
        # We're not using a convolution to start to this does nothing
        model["embedding_prev"] = torch.cuda.FloatTensor(self.n_out_channels,
                                              self.n_residual_channels).fill_(0.0)

        model["embedding_curr"] = self.embed.weight.data
        model["conv_out_weight"] = self.conv_out.conv.weight.data
        model["conv_end_weight"] = self.conv_end.conv.weight.data
        
        dilate_weights = []
        dilate_biases = []
        for layer in self.dilate_layers:
            dilate_weights.append(layer.conv.weight.data)
            dilate_biases.append(layer.conv.bias.data)
        model["dilate_weights"] = dilate_weights
        model["dilate_biases"] = dilate_biases
       
        model["max_dilation"] = self.max_dilation

        res_weights = []
        res_biases = []
        for layer in self.res_layers:
            res_weights.append(layer.conv.weight.data)
            res_biases.append(layer.conv.bias.data)
        model["res_weights"] = res_weights
        model["res_biases"] = res_biases
        
        skip_weights = []
        skip_biases = []
        for layer in self.skip_layers:
            skip_weights.append(layer.conv.weight.data)
            skip_biases.append(layer.conv.bias.data)
        model["skip_weights"] = skip_weights
        model["skip_biases"] = skip_biases
        
        model["use_embed_tanh"] = False
    
        return model

    def get_cond_input(self, features, context_features=None, nv_wavenet=False):
        """
        Takes in features and upsamples them
        If context features present, concatonates the two
        If outputting for nv_wavenet, also does the conditioning transform: cond -> 2*R x batch x # layers x samples tensor
        """

        cond_input = self.upsample(features)
        time_cutoff = self.upsample.kernel_size[0] - self.upsample.stride[0]
        cond_input = cond_input[:, :, :-time_cutoff]

        if (context_features is not None):
            assert(cond_input.size(-1) == context_features.size(-1))
            cond_input = torch.cat([cond_input, context_features], dim=1)

        if (nv_wavenet):
            # This makes the data channels x batch x num_layers x samples
            cond_input = self.cond_layers(cond_input).data
            cond_input = cond_input.view(cond_input.size(0), self.n_layers, -1, cond_input.size(2))
            cond_input = cond_input.permute(2,0,1,3)

        return cond_input
