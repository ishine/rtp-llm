from typing import List, Any, Dict
import torch

from maga_transformer.config.gpt_init_model_parameters import GptInitModelParameters
from maga_transformer.utils.util import get_config_from_path
from maga_transformer.tokenizer.tokenization_chatglm2 import ChatGLMTokenizer
from maga_transformer.models.glm_v2_weight import GlmV2WeightInfo
from maga_transformer.models.gpt import GPT
from maga_transformer.model_factory_register import register_model

class ChatGlmV2(GPT):
    def load_tokenizer(self):
        self.tokenizer = None
        if self.config.tokenizer_path:
            self.tokenizer = ChatGLMTokenizer.from_pretrained(self.config.tokenizer_path)
            self.config.special_tokens.eos_token_id = self.tokenizer.tokenizer.eos_id

    @staticmethod
    def get_weight_cls():
        return GlmV2WeightInfo

    @staticmethod
    def from_huggingface(cls, config_json: Dict[str, Any]):
        '''
        "apply_query_key_layer_scaling": true,
        "apply_residual_connection_post_layernorm": false,
        "attention_softmax_in_fp32": true,
        "fp32_residual_connection": false,
        "original_rope": true,
        '''
        config = GptInitModelParameters(head_num=32,
                                        size_per_head= 128,
                                        layer_num=32,
                                        max_seq_len=8192,
                                        vocab_size=65024)
        config.head_num = config_json['num_attention_heads']
        if config_json.get('multi_query_attention', False):
            config.head_num_kv = config_json['multi_query_group_num']
        else:
            config.head_num_kv = config.head_num
        config.size_per_head = config_json['hidden_size'] // config_json['num_attention_heads']
        config.layer_num = config_json['num_layers']
        config.max_seq_len = config_json.get('seq_length', 8192)
        config.vocab_size = config_json['padded_vocab_size']
        config.weights_data_type = config_json['torch_dtype']
        config.layernorm_eps = config_json['layernorm_epsilon']
        config.inter_size = config_json['ffn_hidden_size']
        config.add_bias_linear = config_json['add_bias_linear']
        config.has_post_decoder_layernorm = config_json['post_layer_norm']
        if 'pre_seq_len' in config_json:
            config.pre_seq_len = config_json['pre_seq_len']
        if 'prefix_projection' in config_json:
            config.prefix_projection = config_json['prefix_projection']
        config.special_tokens.eos_token_id = config_json['eos_token_id']
        config.src_quantization_bit = config_json.get('quantization_bit', 0)
        config = cls.get_rotary_embedding_scale(config, config_json)
        return config
    
    @staticmethod
    def get_rotary_embedding_scale(config, config_json):   
        config.position_embeddings_scale = int(config_json.get("rope_ratio", 1))
        return config
    
    @staticmethod
    def default_config():
        config = GptInitModelParameters(head_num=32,
                                            head_num_kv=2,
                                            size_per_head= 128,
                                            layer_num=32,
                                            max_seq_len=8192,
                                            vocab_size=65024,
                                            layernorm_eps=1e-5,
                                            inter_size=13696,
                                            add_bias_linear=False,
                                            has_post_decoder_layernorm=False)
        return config

    @staticmethod
    def modify_config(config):
        config.special_tokens.eos_token_id = 2
        config.use_attention_linear_bias = False
        config.activation_type = "SiGLU"
        config.norm_type = "rmsnorm"
        config.rotary_embedding_dim = 128
        config.rotary_embedding_style = 3
        
        return config

    @staticmethod
    def _create_config(ckpt_path: str):
        config_dict = get_config_from_path(ckpt_path)
        if config_dict is not None:
            config = ChatGlmV2.from_huggingface(ChatGlmV2, config_dict)
        else:
            config = ChatGlmV2.default_config()
        config = ChatGlmV2.modify_config(config)
        return config

register_model('chatglm2', ChatGlmV2)
register_model('chat_glm_2', ChatGlmV2)