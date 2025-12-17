import os

from dotenv import load_dotenv
from openai import OpenAI

from llms.base_llm import BaseLLM


class ChatOpenAI(BaseLLM):
    def __init__(self, model_name=None, remove_think=False, extra_body=None, **kwargs):
        super().__init__(model_name=model_name, extra_body=extra_body)

        load_dotenv()

        self.client = OpenAI(
            base_url=os.environ.get('OPENAI_BASE_URL'),
            api_key=os.environ['OPENAI_API_KEY']
        )
        self.model_name = model_name or os.environ['OPENAI_MODEL_NAME']
        self.remove_think = remove_think
        self.kwargs = kwargs

    def chat(self, prompt, **kwargs):
        self.kwargs.update(kwargs)
        kwargs = self.kwargs
        self.logger.info(f"chat kwargs: {kwargs}")

        resp = self.client.chat.completions.create(
            model=self.model_name,
            messages=[{'role': 'user', 'content': prompt}] if isinstance(prompt, str) else prompt,
            **kwargs
        )
        if self.remove_think:
            return resp.choices[0].message.content.split('</think>')[-1]
        return resp.choices[0].message.content

    def stream_chat(self, prompt, **kwargs):
        self.kwargs.update(kwargs)
        kwargs = self.kwargs
        self.logger.info(f"chat kwargs: {kwargs}")

        resp = self.client.chat.completions.create(
            model=self.model_name,
            messages=[{'role': 'user', 'content': prompt}] if isinstance(prompt, str) else prompt,
            stream=True,
            # 可选，配置以后会在流式输出的最后一行展示token使用信息
            stream_options={"include_usage": True},
            **kwargs
        )
        for chunk in resp:
            if len(chunk.choices) == 0 or chunk.choices[0].delta.content is None:
                continue
            yield chunk.choices[0].delta.content


if __name__ == '__main__':
    llm = ChatOpenAI()
    print(llm.chat('你是谁', temperature=0.01))

    # prompt = '你是谁'
    # for chunk in llm.stream_chat(prompt):
    #     print(chunk, end='')
