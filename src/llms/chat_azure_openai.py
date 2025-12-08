"""
 Created by Steven Luo on 2025/8/6
"""

import os

from dotenv import load_dotenv
from openai import AzureOpenAI

from llms.chat_openai import ChatOpenAI


class ChatAzureOpenAI(ChatOpenAI):
    def __init__(self, model_name=None, remove_think=False, extra_body=None, **kwargs):
        super().__init__(model_name=model_name, extra_body=extra_body)

        load_dotenv()

        self.client = AzureOpenAI(
            api_key=os.environ['AZURE_API_KEY'],
            azure_endpoint=os.environ['AZURE_ENDPOINT'],
            azure_deployment=os.environ['AZURE_DEPLOYMENT'],
            api_version=os.environ['AZURE_API_VERSION'],
        )
        self.model_name = model_name or os.environ['AZURE_DEPLOYMENT']
        self.remove_think = remove_think
        self.kwargs = kwargs

if __name__ == '__main__':
    llm = ChatAzureOpenAI()
    print(llm.chat('你是谁', temperature=0.01))

    # prompt = '你是谁'
    # for chunk in llm.stream_chat(prompt):
    #     print(chunk, end='')