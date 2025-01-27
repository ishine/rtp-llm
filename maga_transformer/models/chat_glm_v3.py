from typing import List, Any, Dict
import torch

from maga_transformer.utils.util import get_config_from_path
from maga_transformer.models.chat_glm_v2 import ChatGlmV2
from maga_transformer.tokenizer.tokenization_chatglm3 import ChatGLMTokenizer
from maga_transformer.model_factory_register import register_model

class ChatGlmV3(ChatGlmV2):
    def load_tokenizer(self):
        self.tokenizer = None
        if self.config.tokenizer_path:
            self.tokenizer = ChatGLMTokenizer.from_pretrained(self.config.tokenizer_path, encode_special_tokens=True)
            self.config.special_tokens.eos_token_id = self.tokenizer.tokenizer.eos_id

    @staticmethod
    def _create_config(ckpt_path: str):
        config_dict = get_config_from_path(ckpt_path)
        if config_dict is not None:
            config = ChatGlmV3.from_huggingface(ChatGlmV3, config_dict)
        else:
            config = ChatGlmV2.default_config()
        config = ChatGlmV2.modify_config(config)

        return config

    @staticmethod
    def get_rotary_embedding_scale(config, config_json):
        config.position_embeddings_scale = 1
        config.base_scale = int(config_json.get("rope_ratio", 1))
        return config

register_model('chatglm3', ChatGlmV3)
register_model('chat_glm_3', ChatGlmV3)
