/*
 * Copyright (c) 2019-2023, NVIDIA CORPORATION.  All rights reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "src/fastertransformer/layers/TensorParallelFfnLayer.h"
#include "src/fastertransformer/utils/nvtx_utils.h"

namespace fastertransformer {

template<typename T>
void TensorParallelFfnLayer<T>::forward(std::vector<fastertransformer::Tensor>*       output_tensors,
                                        const std::vector<fastertransformer::Tensor>* input_tensors,
                                        const FfnWeight<T>*                           ffn_weights) {
    TensorMap input_tensor({{"ffn_input", input_tensors->at(0)}});
    TensorMap output_tensor({{"ffn_output", output_tensors->at(0)}});
    forward(&output_tensor, &input_tensor, ffn_weights);
}

template<typename T>
void TensorParallelFfnLayer<T>::forward(Tensor&             ffn_output,
                                        const Tensor&       ffn_input,
                                        int                 layer_id,
                                        const Tensor&       lora_ids,
                                        const Tensor&       lora_input_lengths,
                                        int                 ffn_batch_size_lora,
                                        const FfnWeight<T>* ffn_weights) {
    TensorMap input_tensor({{"ffn_input", ffn_input},
                            {"layer_id", Tensor{MEMORY_CPU, TYPE_INT32, {(size_t)1}, &layer_id}},
                            {"lora_ids", lora_ids},
                            {"lora_input_lengths", lora_input_lengths},
                            {"batch_size", Tensor{MEMORY_CPU, TYPE_INT32, {(size_t)1}, &ffn_batch_size_lora}}});
    TensorMap output_tensor({{"ffn_output", ffn_output}});
    forward(&output_tensor, &input_tensor, ffn_weights);
}

template<typename T>
void TensorParallelFfnLayer<T>::forward(TensorMap*          output_tensors,
                                        TensorMap*          input_tensors,
                                        const FfnWeight<T>* ffn_weights) {
    FT_LOG_DEBUG("%s start", __PRETTY_FUNCTION__);
    Tensor       out_tensor   = output_tensors->at("ffn_output");
    const size_t token_num    = out_tensor.shape[0];
    const size_t hidden_units = out_tensor.shape[1];

    std::vector<Tensor> swap_tensors = {out_tensor};

    bool use_custom_all_reduce_kernel = false;
    if (enable_custom_all_reduce_ && custom_all_reduce_comm_ != nullptr) {
        use_custom_all_reduce_kernel =
            custom_all_reduce_comm_->swapInternalBuffer(&swap_tensors, token_num * hidden_units);
    }

    FfnLayer<T>::forward(output_tensors, input_tensors, ffn_weights);

    // PUSH_RANGE(stream_, "FFN all reduce sum");
    T* ffn_out = out_tensor.getPtr<T>();
    if (do_all_reduce_ && tensor_para_.world_size_ > 1) {
        if (!use_custom_all_reduce_kernel) {
            ftNcclAllReduceSum(ffn_out, ffn_out, token_num * hidden_units, tensor_para_, FfnLayer<T>::stream_);
        } else {
            custom_all_reduce_comm_->customAllReduce(token_num * hidden_units, FfnLayer<T>::stream_);
        }
        sync_check_cuda_error();
    }
    // POP_RANGE;
}

template<typename T>
TensorParallelFfnLayer<T>::TensorParallelFfnLayer(size_t                              max_batch_size,
                                                  size_t                              max_seq_len,
                                                  size_t                              head_num,
                                                  size_t                              size_per_head,
                                                  size_t                              expert_num,
                                                  size_t                              inter_size,
                                                  size_t                              inter_padding_size,
                                                  std::vector<int64_t>                layer_inter_size,
                                                  std::vector<int64_t>                layer_inter_padding_size,
                                                  NcclParam                           tensor_para,
                                                  cudaStream_t                        stream,
                                                  cublasMMWrapper*                    cublas_wrapper,
                                                  IAllocator*                         allocator,
                                                  bool                                do_all_reduce,
                                                  bool                                is_free_buffer_after_forward,
                                                  bool                                is_sparse,
                                                  bool                                is_sparse_head,
                                                  int                                 int8_mode,
                                                  ActivationType                      activation_type,
                                                  float                               layernorm_eps,
                                                  std::shared_ptr<AbstractCustomComm> custom_all_reduce_comm,
                                                  int                                 enable_custom_all_reduce):
    FfnLayer<T>(max_batch_size,
                max_seq_len,
                head_num,
                size_per_head,
                expert_num,
                inter_size / tensor_para.world_size_,
                inter_padding_size / tensor_para.world_size_,
                getLocalParameter(layer_inter_size, tensor_para.world_size_),
                getLocalParameter(layer_inter_padding_size, tensor_para.world_size_),
                stream,
                cublas_wrapper,
                allocator,
                is_free_buffer_after_forward,
                is_sparse,
                is_sparse_head,
                int8_mode,
                activation_type,
                layernorm_eps),
    tensor_para_(tensor_para),
    custom_all_reduce_comm_(custom_all_reduce_comm),
    enable_custom_all_reduce_(enable_custom_all_reduce),
    do_all_reduce_(do_all_reduce) {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    FT_CHECK(inter_size % tensor_para_.world_size_ == 0);
}

template<typename T>
TensorParallelFfnLayer<T>::TensorParallelFfnLayer(TensorParallelFfnLayer<T> const& ffn_layer):
    FfnLayer<T>(ffn_layer),
    tensor_para_(ffn_layer.tensor_para_),
    custom_all_reduce_comm_(ffn_layer.custom_all_reduce_comm_),
    enable_custom_all_reduce_(ffn_layer.enable_custom_all_reduce_),
    do_all_reduce_(ffn_layer.do_all_reduce_) {
    FfnLayer<T>::layernorm_eps_ = ffn_layer.layernorm_eps_;
}

template class TensorParallelFfnLayer<float>;
template class TensorParallelFfnLayer<half>;
#ifdef ENABLE_BF16
template class TensorParallelFfnLayer<__nv_bfloat16>;
#endif

}  // namespace fastertransformer
