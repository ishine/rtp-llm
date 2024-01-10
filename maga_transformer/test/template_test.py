import os
import torch
from unittest import TestCase, main
from typing import Any
from transformers import AutoTokenizer, PreTrainedTokenizer

from maga_transformer.pipeline.chatapi_format import encode_chatapi
from maga_transformer.models.starcoder import StarcoderTokenizer
from maga_transformer.openai.api_datatype import ChatMessage, RoleEnum, \
    ChatCompletionRequest, GPTFunctionDefinition, ContentPart, ContentPartTypeEnum
from maga_transformer.tokenizer.tokenization_qwen import QWenTokenizer
from maga_transformer.openai.renderers.renderer_factory import ChatRendererFactory, RendererParams
from maga_transformer.openai.renderers.qwen_vl_renderer import QwenVLRenderer

class ChatapiTest(TestCase):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.test_data_path = os.path.join(
            os.getcwd(), 'maga_transformer/test/model_test/fake_test/testdata'
        )

    def test_qwen(self):
        os.environ["MODEL_TYPE"] = "qwen"
        tokenizer = QWenTokenizer(f"{self.test_data_path}/qwen_7b/tokenizer/qwen.tiktoken")
        render_params = RendererParams(
            max_seq_len=1024,
            eos_token_id=tokenizer.eos_token_id or 0,
            stop_word_ids_list=[],
        )
        chat_renderer = ChatRendererFactory.get_renderer(tokenizer, render_params)

        functions = [
            GPTFunctionDefinition(**{
                "name": "get_current_weather",
                "description": "Get the current weather in a given location.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and state, e.g. San Francisco, CA",
                        },
                        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                    },
                    "required": ["location"],
                },
            })
        ]

        messages = [
            ChatMessage(**{
                "role": RoleEnum.user,
                "content": "波士顿天气如何？",
            }),
        ]

        request = ChatCompletionRequest(**{
            "messages": messages,
            "functions": functions,
            "stream": False,
        })

        ids = chat_renderer.render_chat(request).input_ids
        prompt = tokenizer.decode(ids)
        print(f"rendered prompt: \n{prompt}\n-----------------------------------")
        expected_prompt = \
"""<|im_start|>system
You are a helpful assistant.<|im_end|>
<|im_start|>user
Answer the following questions as best you can. You have access to the following APIs:

get_current_weather: Call this tool to interact with the get_current_weather API. What is the get_current_weather API useful for? Get the current weather in a given location. Parameters: {"type": "object", "properties": {"location": {"type": "string", "description": "The city and state, e.g. San Francisco, CA"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}}, "required": ["location"]}

Use the following format:

Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [get_current_weather]
Action Input: the input to the action
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can be repeated zero or more times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin!

Question: 波士顿天气如何？<|im_end|>
<|im_start|>assistant
"""
        print(f"expected prompt: \n{expected_prompt}\n-----------------------------------")
        assert (prompt == expected_prompt)

        messages.append(
            ChatMessage(**{
                "role": RoleEnum.assistant,
                "content": "",
                "function_call": {
                    "name": "get_current_weather",
                    "arguments": '{"location": "Boston, MA"}',
                },
            })
        )

        messages.append(
            ChatMessage(**{
                "role": RoleEnum.function,
                "name": "get_current_weather",
                "content": '{"temperature": "22", "unit": "celsius", "description": "Sunny"}',
            })
        )

        request.messages = messages
        ids = chat_renderer.render_chat(request).input_ids
        prompt = tokenizer.decode(ids)
        print(f"rendered prompt: \n{prompt}\n-----------------------------------")
        expected_prompt = \
"""<|im_start|>system
You are a helpful assistant.<|im_end|>
<|im_start|>user
Answer the following questions as best you can. You have access to the following APIs:

get_current_weather: Call this tool to interact with the get_current_weather API. What is the get_current_weather API useful for? Get the current weather in a given location. Parameters: {"type": "object", "properties": {"location": {"type": "string", "description": "The city and state, e.g. San Francisco, CA"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}}, "required": ["location"]}

Use the following format:

Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [get_current_weather]
Action Input: the input to the action
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can be repeated zero or more times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin!

Question: 波士顿天气如何？<|im_end|>
<|im_start|>assistant
Thought: 我可以使用 get_current_weather API。
Action: get_current_weather
Action Input: {"location": "Boston, MA"}
Observation: {"temperature": "22", "unit": "celsius", "description": "Sunny"}
Thought:"""
        print(f"expected prompt: \n{expected_prompt}\n-----------------------------------")
        assert (prompt == expected_prompt)

    def test_qwen_vl(self):
        os.environ["MODEL_TYPE"] = "qwen_vl"
        tokenizer = AutoTokenizer.from_pretrained(f"{self.test_data_path}/qwen_vl/tokenizer/", trust_remote_code=True)
        assert(isinstance(tokenizer, PreTrainedTokenizer))
        render_params = RendererParams(
            max_seq_len=1024,
            eos_token_id=tokenizer.eos_token_id or 0,
            stop_word_ids_list=[],
        )
        chat_renderer = ChatRendererFactory.get_renderer(tokenizer, render_params)

        test_messages = [ChatMessage(**{
            "role": RoleEnum.user,
            "content": [
                ContentPart(**{
                    "type": ContentPartTypeEnum.image_url,
                    "image_url": "https://modelscope.cn/api/v1/models/damo/speech_eres2net_sv_zh-cn_16k-common/repo?Revision=master&FilePath=images/ERes2Net_architecture.png"
                }),
                ContentPart(**{
                    "type": ContentPartTypeEnum.text,
                    "text": "这是什么"
                }),
            ],
        })]
        request = ChatCompletionRequest(**{
            "messages": test_messages,
            "stream": False,
        })
        ids = chat_renderer.render_chat(request).input_ids
        expected_ids = [151644, 8948, 198, 2610, 525, 264, 10950, 17847, 13, 151645, 198, 151644, 872, 198, 24669, 220, 16, 25, 220, 151857, 104, 116, 116, 112, 115, 58, 47, 47, 109, 111, 100, 101, 108, 115, 99, 111, 112, 101, 46, 99, 110, 47, 97, 112, 105, 47, 118, 49, 47, 109, 111, 100, 101, 108, 115, 47, 100, 97, 109, 111, 47, 115, 112, 101, 101, 99, 104, 95, 101, 114, 101, 115, 50, 110, 101, 116, 95, 115, 118, 95, 122, 104, 45, 99, 110, 95, 49, 54, 107, 45, 99, 111, 109, 109, 111, 110, 47, 114, 101, 112, 111, 63, 82, 101, 118, 105, 115, 105, 111, 110, 61, 109, 97, 115, 116, 101, 114, 38, 70, 105, 108, 101, 80, 97, 116, 104, 61, 105, 109, 97, 103, 101, 115, 47, 69, 82, 101, 115, 50, 78, 101, 116, 95, 97, 114, 99, 104, 105, 116, 101, 99, 116, 117, 114, 101, 46, 112, 110, 103, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151858, 198, 100346, 99245, 151645, 198, 151644, 77091, 198]
        print(f"------- ids: {ids}")
        assert (ids == expected_ids)

        request.messages.append(ChatMessage(**{
            "role": RoleEnum.assistant,
            "content": "这是一个深度神经网络模型的结构图。从图中可以看出，这个模型包括全局特征融合和局部特征融合两部分。全局特征融合部分使用了ERes2Net block，而局部特征融合部分则使用了多个ERes2Net block和AFF block。此外，模型的最后一层还使用了一个1×1的卷积层。图中还标注了各个模块之间的连接关系，包括输入、输出以及与其他模块的连接。",
        }))
        request.messages.append(ChatMessage(**{
            "role": RoleEnum.user,
            "content": "输出 embedding 层的检测框",
        }))

        ids = chat_renderer.render_chat(request).input_ids
        expected_ids = [151644, 8948, 198, 2610, 525, 264, 10950, 17847, 13, 151645, 198, 151644, 872, 198, 24669, 220, 16, 25, 220, 151857, 104, 116, 116, 112, 115, 58, 47, 47, 109, 111, 100, 101, 108, 115, 99, 111, 112, 101, 46, 99, 110, 47, 97, 112, 105, 47, 118, 49, 47, 109, 111, 100, 101, 108, 115, 47, 100, 97, 109, 111, 47, 115, 112, 101, 101, 99, 104, 95, 101, 114, 101, 115, 50, 110, 101, 116, 95, 115, 118, 95, 122, 104, 45, 99, 110, 95, 49, 54, 107, 45, 99, 111, 109, 109, 111, 110, 47, 114, 101, 112, 111, 63, 82, 101, 118, 105, 115, 105, 111, 110, 61, 109, 97, 115, 116, 101, 114, 38, 70, 105, 108, 101, 80, 97, 116, 104, 61, 105, 109, 97, 103, 101, 115, 47, 69, 82, 101, 115, 50, 78, 101, 116, 95, 97, 114, 99, 104, 105, 116, 101, 99, 116, 117, 114, 101, 46, 112, 110, 103, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151859, 151858, 198, 100346, 99245, 151645, 198, 151644, 77091, 198, 105464, 102217, 102398, 71356, 104949, 9370, 100166, 28029, 1773, 45181, 28029, 15946, 107800, 3837, 99487, 104949, 100630, 109894, 104363, 101164, 33108, 106304, 104363, 101164, 77540, 99659, 1773, 109894, 104363, 101164, 99659, 37029, 34187, 640, 288, 17, 6954, 2504, 3837, 68536, 106304, 104363, 101164, 99659, 46448, 37029, 34187, 101213, 640, 288, 17, 6954, 2504, 33108, 48045, 2504, 1773, 104043, 3837, 104949, 114641, 99371, 97706, 37029, 104059, 16, 17568, 16, 9370, 100199, 99263, 99371, 1773, 28029, 15946, 97706, 111066, 34187, 101284, 106393, 104186, 64064, 100145, 3837, 100630, 31196, 5373, 66017, 101034, 106961, 106393, 9370, 64064, 1773, 151645, 198, 151644, 872, 198, 66017, 39088, 79621, 224, 9370, 101978, 101540, 151645, 198, 151644, 77091, 198]
        print(f"------- ids: {ids}")
        assert (ids == expected_ids)

if __name__ == '__main__':
    main()