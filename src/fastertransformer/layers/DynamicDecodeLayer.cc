/*
 * Copyright (c) 2022-2022, NVIDIA CORPORATION.  All rights reserved.
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

#include "src/fastertransformer/layers/DynamicDecodeLayer.h"
#include "src/fastertransformer/kernels/ban_bad_words.h"
#include "src/fastertransformer/kernels/stop_criteria_kernels.h"
#include "src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.h"
#include "src/fastertransformer/layers/beam_search_layers/BeamSearchLayer.h"
#include "src/fastertransformer/layers/beam_search_layers/OnlineBeamSearchLayer.h"
#include "src/fastertransformer/layers/sampling_layers/TopKSamplingLayer.h"
#include "src/fastertransformer/layers/sampling_layers/TopPSamplingLayer.h"
#include "src/fastertransformer/utils/nvtx_utils.h"

namespace fastertransformer {

template<typename T>
void DynamicDecodeLayer<T>::allocateBuffer() {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    h_pinned_finished_sum_ = (int*)allocator_->reMalloc(h_pinned_finished_sum_, sizeof(int), true, true);
    return;
}

template<typename T>
void DynamicDecodeLayer<T>::freeBuffer() {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    allocator_->free((void**)(&h_pinned_finished_sum_), true);
    return;
}

template<typename T>
void DynamicDecodeLayer<T>::initialize() {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    online_beamsearch_decode_ = new OnlineBeamSearchLayer<T>(0,  // max_batch_size, deprecated
                                                             0,  // local_head_num, deprecated
                                                             0,  // size_per_head, deprecated
                                                             0,  // beam_width, deprecated
                                                             vocab_size_,
                                                             vocab_size_padded_,
                                                             0,     // end_id, deprecated
                                                             0.0f,  // beam_search_diversity_rate_, deprecated
                                                             1.0f,  // temperature_, deprecated
                                                             0.0f,  // len_penalty_, deprecated
                                                             1.0f,  // repetition_penalty_, deprecated
                                                             stream_,
                                                             cublas_wrapper_,
                                                             allocator_,
                                                             is_free_buffer_after_forward_);

    topk_decode_ = new TopKSamplingLayer<T>(0,
                                            vocab_size_,
                                            vocab_size_padded_,
                                            0,     // end_id, deprecated
                                            0,     // top_k_, deprecated
                                            0,     // random_seed_, deprecated
                                            1.0f,  // temperature_, deprecated
                                            0.0f,  // len_penalty_, deprecated
                                            1.0f,  // repetition_penalty_, deprecated
                                            stream_,
                                            cublas_wrapper_,
                                            allocator_,
                                            false);

    topp_decode_ = new TopPSamplingLayer<T>(0,
                                            vocab_size_,
                                            vocab_size_padded_,
                                            0,     // end_id, deprecated
                                            0.0f,  // top_p_, deprecated
                                            0,     // random_seed_, deprecated
                                            1.0f,  // temperature_, deprecated
                                            0.0f,  // len_penalty_, deprecated
                                            1.0f,  // repetition_penalty_, deprecated
                                            stream_,
                                            cublas_wrapper_,
                                            allocator_,
                                            false,
                                            cuda_device_prop_);

    allocateBuffer();
}

template<typename T>
DynamicDecodeLayer<T>::DynamicDecodeLayer(size_t           vocab_size,
                                          size_t           vocab_size_padded,
                                          int              end_id,
                                          cudaStream_t     stream,
                                          cublasMMWrapper* cublas_wrapper,
                                          IAllocator*      allocator,
                                          bool             is_free_buffer_after_forward,
                                          cudaDeviceProp*  cuda_device_prop):
    BaseLayer(stream, cublas_wrapper, allocator, is_free_buffer_after_forward),
    vocab_size_(vocab_size),
    vocab_size_padded_(vocab_size_padded),
    cuda_device_prop_(cuda_device_prop) {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    initialize();
}

template<typename T>
DynamicDecodeLayer<T>::~DynamicDecodeLayer() {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    delete online_beamsearch_decode_;
    delete topk_decode_;
    delete topp_decode_;
    freeBuffer();
}

template<typename T>
DynamicDecodeLayer<T>::DynamicDecodeLayer(DynamicDecodeLayer const& dynamic_decode_layer):
    BaseLayer(dynamic_decode_layer),
    vocab_size_(dynamic_decode_layer.vocab_size_),
    vocab_size_padded_(dynamic_decode_layer.vocab_size_padded_),
    cuda_device_prop_(dynamic_decode_layer.cuda_device_prop_) {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    initialize();
}

template<typename T>
void DynamicDecodeLayer<T>::setup(const size_t batch_size, const size_t beam_width, TensorMap* runtime_args) {
    /**
     * @brief Set up the dynamic decode layer for given input runtime arguments.
     *
     * runtime_args:
     *   \param  runtime_top_k [1] or [batch_size] on cpu, optional.
     *   \param  runtime_top_p [1] or [batch_size] on cpu, optional
     *   \param  beam_search_diversity_rate [1] or [batch_size] on cpu, optional
     *   \param  temperature [1] or [batch_size] on cpu, optional
     *   \param  len_penalty [1] or [batch_size] on cpu, optional
     *   \param  repetition_penalty [1] or [batch_size] on cpu, optional
     *   \param  presence_penalty [1] or [batch_size] on cpu, optional, float
     *   \param  min_length [1] or [batch_size], optional
     *   \param  top_p_decay [batch_size] on gpu, float, optional
     *   \param  top_p_min [batch_size] on gpu, float, optional
     *   \param  top_p_reset_ids [batch_size] on gpu, uint32, optional
     */

    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    PUSH_RANGE(stream_, "dynamic_decoder_set_up");
    has_diff_runtime_args_ = hasDiffRuntimeArgs(runtime_args);
    if (beam_width == 1) {  // sampling layers
        topk_decode_->setup(batch_size, beam_width, runtime_args);
        topp_decode_->setup(batch_size, beam_width, runtime_args);
    } else {
        online_beamsearch_decode_->setup(batch_size, beam_width, runtime_args);
    }
    POP_RANGE;
    final_check_error();
}

template<typename T>
void DynamicDecodeLayer<T>::forward(std::unordered_map<std::string, Tensor>*       output_tensors,
                                    const std::unordered_map<std::string, Tensor>* input_tensors) {
    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    TensorMap input_map(*input_tensors);
    TensorMap output_map(*output_tensors);
    forward(&output_map, &input_map);
    final_check_error();
}

template<typename T>
void DynamicDecodeLayer<T>::forward(TensorMap* output_tensors, TensorMap* input_tensors) {
    /**
     * @brief
     * input_tensors:
     *   \param  logits [batch_size, beam_width, vocab_size_padded]
     *   \param  embedding_bias [vocab_size_padded], optional
     *   \param  step [1] on cpu
     *   \param  max_input_length [1] on cpu
     *   \param  input_lengths [batch_size, beam_width], optional
     *   \param  min_length [batch_size], optional
     *   \param  sequence_limit_length [batch_size]
     *   \param  ite [1] on cpu
     *   \param  local_batch_size [1] on cpu
     *   \param  stop_words_list [batch_size, 2, stop_words_length], optional
     *   \param  runtime_top_k [1] or [batch_size] on cpu, optional, uint
     *   \param  runtime_top_p [1] or [batch_size] on cpu, optional, float
     *   \param  temperature [1] or [batch_size] on cpu, optional, float
     *   \param  len_penalty [1] or [batch_size] on cpu, optional, float
     *   \param  repetition_penalty [1] or [batch_size] on cpu, optional, float
     *   \param  presence_penalty [1] or [batch_size] on cpu, optional, float
     *                Only one of repetition and presence penalties is allowed.
     *   \param  random_seed [1] or [batch_size] on cpu, optional, unsigned long long int
     *   \param  bad_words_list [2, bad_words_length] or [batch_size, 2, bad_words_length], optional
     *   \param  src_cache_indirection
     *                [local_batch_size, beam_width, max_seq_len]
     *                the k/v cache index for beam search
     *   \param  is_initialize_random_table [1] on cpu, bool
     *   \param  top_p_decay [batch_size] on gpu, float, optional
     *   \param  top_p_min [batch_size] on gpu, float, optional
     *   \param  top_p_reset_ids [batch_size] on gpu, uint32, optional
     *
     * output_tensors:
     *   \param  output_ids [max_seq_len, batch_size]
     *   \param  finished [batch_size * beam_width], optional
     *   \param  should_stop [1] on cpu
     *   \param  cum_log_probs [batch_size * beam_width], necessary in beam search
     *   \param  parent_ids [max_seq_len, batch_size * beam_width]
     *   \param  sequence_length [batch_size * beam_width], optional
     *   \param  output_log_probs [request_ouptut_length, batch_size * beam_width], must be float*, optional
     *   \param  tgt_cache_indirection
     *                [local_batch_size, beam_width, max_seq_len]
     *                the k/v cache index for beam search
     *   \param  beam_hyps: [1] on cpu, a special structure which maintains some pointers of beam search
     *
     */

    FT_LOG_DEBUG(__PRETTY_FUNCTION__);
    const int ite  = (int)input_tensors->at("ite").getVal<uint>();
    const int step = input_tensors->at("step").getVal<int>();
    FT_CHECK(input_tensors->at("logits").shape.size() == 3);

    const size_t batch_size       = input_tensors->at("logits").shape[0];
    const size_t beam_width       = input_tensors->at("logits").shape[1];
    const size_t local_batch_size = (size_t)input_tensors->at("local_batch_size").getVal<int>();

    if (input_tensors->isExist("bad_words_list")) {
        const auto& bad_words     = input_tensors->at("bad_words_list");
        const int*  bad_words_ptr = bad_words.getPtr<const int>();
        FT_CHECK_WITH_INFO(bad_words.shape.size() == 2 || bad_words.shape.size() == 3,
                           "Bad words dimension must be 2 or 3.");

        const bool is_matrix = bad_words.shape.size() == 2;
        if (bad_words.shape.size() == 3) {
            FT_CHECK_WITH_INFO(bad_words.shape[0] == batch_size,
                               fmtstr("Shape of dim 0 of bad words is invalid. It must be equal to batch size."
                                      " However, it is %d and the batch size is %d.",
                                      bad_words.shape[0],
                                      batch_size));
        }

        const bool   shared_bad_words = is_matrix || bad_words.shape[0] == 1;
        const size_t bad_words_len    = bad_words.shape[is_matrix ? 1 : 2];
        // Add check on batch size of bad words
        const int id_offset                      = ite * local_batch_size;
        const int decode_vocab_size_units_offset = id_offset * vocab_size_padded_;

        invokeBanBadWords((T*)input_tensors->at("logits").getPtrWithOffset(decode_vocab_size_units_offset),
                          output_tensors->at("output_ids").getPtr<const int>(),
                          beam_width > 1 ? output_tensors->at("parent_ids").getPtr<const int>() : nullptr,
                          batch_size,
                          local_batch_size,
                          beam_width,
                          shared_bad_words ?
                              bad_words_ptr :
                              bad_words.getPtrWithOffset<const int>(ite * local_batch_size * 2 * bad_words_len),
                          shared_bad_words,
                          bad_words_len,
                          id_offset,
                          vocab_size_padded_,
                          step,
                          stream_);
    }

    // dynamic decode GPT
    if (beam_width > 1) {
        // Because we still not support batch beam search now, so we need to compute one by one if there are different
        // runtime arguments.
        const size_t dynamic_decode_batch_size      = has_diff_runtime_args_ ? 1 : local_batch_size;
        const int    dynamic_decode_total_iteration = local_batch_size / dynamic_decode_batch_size;

        for (uint dynamic_ite = ite * dynamic_decode_total_iteration;
             dynamic_ite < (ite + 1) * dynamic_decode_total_iteration;
             ++dynamic_ite) {
            const int dynamic_id_offset                      = dynamic_ite * dynamic_decode_batch_size * beam_width;
            const int dynamic_decode_vocab_size_units_offset = dynamic_id_offset * vocab_size_padded_;

            // common inputs
            Tensor logits = input_tensors->at("logits");
            Tensor end_id = input_tensors->at("end_id");

            TensorMap dynamic_decode_input_tensors(
                {{"logits",
                  Tensor{logits.where,
                         logits.type,
                         {dynamic_decode_batch_size, logits.shape[1], logits.shape[2]},
                         logits.getPtrWithOffset(dynamic_decode_vocab_size_units_offset)}},
                 {"step", input_tensors->at("step")},
                 {"max_input_length", input_tensors->at("max_input_length")},
                 {"end_id",
                  Tensor{end_id.where,
                         end_id.type,
                         {dynamic_decode_batch_size},
                         end_id.getPtrWithOffset(dynamic_ite * dynamic_decode_batch_size)}},
                 {"ite", Tensor{MEMORY_CPU, TYPE_UINT32, {1}, &dynamic_ite}}});

            if (input_tensors->isExist("embedding_bias")) {
                dynamic_decode_input_tensors.insert({"embedding_bias", input_tensors->at("embedding_bias")});
            }
            if (input_tensors->isExist("input_lengths")) {
                Tensor input_lengths = input_tensors->at("input_lengths");
                dynamic_decode_input_tensors.insert(
                    {"input_lengths",
                     input_lengths.slice({dynamic_decode_batch_size, input_lengths.shape[1]}, dynamic_id_offset)});
            }
            for (auto t = input_tensors->begin(); t != input_tensors->end(); ++t) {
                if (t->first.find("random_seed") == std::string::npos) {
                    dynamic_decode_input_tensors.insert(*t);
                }
            }

            // common outputs
            TensorMap dynamic_decode_output_tensors({{"output_ids", output_tensors->at("output_ids")}});
            if (output_tensors->isExist("sequence_length")) {
                Tensor sequence_length = output_tensors->at("sequence_length");
                dynamic_decode_output_tensors.insert({"sequence_length",
                                                      Tensor{sequence_length.where,
                                                             sequence_length.type,
                                                             {dynamic_decode_batch_size * beam_width},
                                                             sequence_length.getPtrWithOffset(dynamic_id_offset)}});
            }
            if (output_tensors->isExist("finished")) {
                Tensor finished = output_tensors->at("finished");
                dynamic_decode_output_tensors.insert({"finished",
                                                      Tensor{finished.where,
                                                             finished.type,
                                                             {dynamic_decode_batch_size * beam_width},
                                                             finished.getPtrWithOffset(dynamic_id_offset)}});
            }
            if (output_tensors->isExist("cum_log_probs")) {
                Tensor cum_log_probs = output_tensors->at("cum_log_probs");
                dynamic_decode_output_tensors.insert({"cum_log_probs",
                                                      Tensor{cum_log_probs.where,
                                                             cum_log_probs.type,
                                                             {dynamic_decode_batch_size * beam_width},
                                                             cum_log_probs.getPtrWithOffset(dynamic_id_offset)}});
            }

            if (output_tensors->isExist("beam_hyps")) {
                dynamic_decode_output_tensors.insert("beam_hyps", output_tensors->at("beam_hyps"));
            }

            if (output_tensors->isExist("output_log_probs")) {
                dynamic_decode_output_tensors.insert({"output_log_probs", output_tensors->at("output_log_probs")});
            }

            dynamic_decode_input_tensors.insert({"src_cache_indirection", input_tensors->at("src_cache_indirection")});

            dynamic_decode_output_tensors.insert({"parent_ids", output_tensors->at("parent_ids")});
            dynamic_decode_output_tensors.insert(
                {"tgt_cache_indirection", output_tensors->at("tgt_cache_indirection")});
            dynamic_decode_output_tensors.insert({"normed_scores", output_tensors->at("normed_scores")});
            dynamic_decode_output_tensors.insert({"min_normed_scores", output_tensors->at("min_normed_scores")});
            dynamic_decode_output_tensors.insert({"num_beams", output_tensors->at("num_beams")});

            FT_CHECK_WITH_INFO(dynamic_decode_output_tensors.isExist("cum_log_probs"),
                               "cum_log_probs should be provided in beam search.");

            online_beamsearch_decode_->forward(&dynamic_decode_output_tensors, &dynamic_decode_input_tensors);
        }     // end of dynamic_ite
    } else {  // beam_width=1
        // In sampling, we have supported batch sampling. So, we always compute all sentences once.
        const size_t local_batch_offset = ite * local_batch_size * beam_width;

        Tensor logits = input_tensors->at("logits");
        Tensor end_id = input_tensors->at("end_id");

        TensorMap decode_input_tensors(
            {{"logits",
              logits.slice({local_batch_size, beam_width, logits.shape[2]}, local_batch_offset * logits.shape[2])},
             {"step", input_tensors->at("step")},
             {"max_input_length", input_tensors->at("max_input_length")},
             {"end_id", end_id.slice({local_batch_size}, ite * local_batch_size)},
             {"ite", Tensor{MEMORY_CPU, TYPE_INT32, {1}, &ite}}});

        if (input_tensors->isExist("embedding_bias")) {
            decode_input_tensors.insert({"embedding_bias", input_tensors->at("embedding_bias")});
        }
        if (input_tensors->isExist("input_lengths")) {
            Tensor input_lengths = input_tensors->at("input_lengths");
            decode_input_tensors.insert(
                {"input_lengths", input_lengths.slice({local_batch_size, beam_width}, local_batch_offset)});
        }

        TensorMap decode_output_tensors({{"output_ids", output_tensors->at("output_ids")}});
        if (output_tensors->isExist("sequence_length")) {
            Tensor sequence_length = output_tensors->at("sequence_length");
            decode_output_tensors.insert(
                {"sequence_length", sequence_length.slice({local_batch_size * beam_width}, local_batch_offset)});
        }
        if (output_tensors->isExist("finished")) {
            Tensor finished = output_tensors->at("finished");
            decode_output_tensors.insert(
                {"finished", finished.slice({local_batch_size * beam_width}, local_batch_offset)});
        }
        if (output_tensors->isExist("cum_log_probs")) {
            Tensor cum_log_probs = output_tensors->at("cum_log_probs");
            decode_output_tensors.insert(
                {"cum_log_probs", cum_log_probs.slice({local_batch_size * beam_width}, local_batch_offset)});
        }
        if (output_tensors->isExist("output_log_probs")) {
            Tensor output_log_probs = output_tensors->at("output_log_probs");
            int    max_input_length = input_tensors->at("max_input_length").getVal<int>();
            size_t step_offset      = (step - max_input_length) * batch_size * beam_width;
            decode_output_tensors.insert(
                {"output_log_probs", output_log_probs.slice({local_batch_size * beam_width}, local_batch_offset)});
        }
        if (output_tensors->isExist("index_log_probs") && output_tensors->isExist("token_id_for_index_prob")) {
            Tensor index_log_probs = output_tensors->at("index_log_probs");
            decode_output_tensors.insert(
                {"index_log_probs", index_log_probs.slice({local_batch_size * beam_width}, local_batch_offset)});
            Tensor token_id_for_index_prob = output_tensors->at("token_id_for_index_prob");
            decode_output_tensors.insert(
                {"token_id_for_index_prob",
                 token_id_for_index_prob.slice({local_batch_size * beam_width}, local_batch_offset)});
        }

        // Run topk / topp decode layers.
        // Currently, we support batch sampling. If the runtime arguments are like
        // topk = [4, 0, 4]. topp = [0.0, 0.5, 0.5]
        // then topk_decode handles [4, x, 4 + 0.5]
        //      topp_decode handles [x, 0.5, x]
        // where "x" are skipped.
        PUSH_RANGE(stream_, "dynamic_decoder");
        topk_decode_->forward(&decode_output_tensors, &decode_input_tensors);
        topp_decode_->forward(&decode_output_tensors, &decode_input_tensors);
        POP_RANGE;
    }

    if (input_tensors->isExist("stop_words_list")) {
        const size_t id_offset         = ite * local_batch_size * beam_width;
        const size_t stop_words_length = input_tensors->at("stop_words_list").shape[2];

        const int* parent_ids_ptr = nullptr;
        if (beam_width > 1) {
            parent_ids_ptr = output_tensors->at("parent_ids").getPtr<const int>();
        }
        PUSH_RANGE(stream_, "stop_word");
        invokeStopWordsCriterion(output_tensors->at("output_ids").getPtr<const int>(),
                                 parent_ids_ptr,
                                 input_tensors->at("stop_words_list")
                                     .getPtrWithOffset<const int>(ite * local_batch_size * 2 * stop_words_length),
                                 output_tensors->at("finished").getPtrWithOffset<bool>(id_offset),
                                 id_offset,
                                 stop_words_length,
                                 batch_size,
                                 beam_width,
                                 step,
                                 stream_);
        POP_RANGE;
    }

    if (input_tensors->isExist("sequence_limit_length")) {
        PUSH_RANGE(stream_, "length_criterion");
        invokeLengthCriterion(output_tensors->at("finished").getPtr<bool>(),
                              output_tensors->at("should_stop").getPtr<bool>(),
                              h_pinned_finished_sum_,
                              input_tensors->at("sequence_limit_length").getPtr<const uint32_t>(),
                              batch_size,
                              beam_width,
                              step,
                              stream_);
        POP_RANGE;
    }
}

template<typename T>
bool DynamicDecodeLayer<T>::hasDiffRuntimeArgs(TensorMap* input_tensors) {
    for (int i = 0; i < (int)runtime_arg_names_.size(); i++) {
        if (input_tensors->isExist(runtime_arg_names_[i])) {
            auto tensor = input_tensors->at(runtime_arg_names_[i]);
            FT_CHECK(tensor.shape.size() == 1);
            for (int j = 1; j < (int)tensor.shape[0]; j++) {
                const void* data = tensor.data;
                switch (tensor.type) {
                    case TYPE_FP32:
                        if (((const float*)data)[0] != ((const float*)data)[j]) {
                            return true;
                        }
                        break;
                    case TYPE_INT32:
                        if (((const int*)data)[0] != ((const int*)data)[j]) {
                            return true;
                        }
                        break;
                    case TYPE_UINT32:
                        if (((const uint*)data)[0] != ((const uint*)data)[j]) {
                            return true;
                        }
                        break;
                    case TYPE_UINT64:
                        if (((const unsigned long long int*)data)[0] != ((const unsigned long long int*)data)[j]) {
                            return true;
                        }
                        break;
                    default:
                        FT_CHECK_WITH_INFO(false, runtime_arg_names_[i] + ": " + tensor.toString() + " is invalid.");
                        break;
                }
            }
        }
    }
    return false;
}

template class DynamicDecodeLayer<float>;
template class DynamicDecodeLayer<half>;

}  // namespace fastertransformer
