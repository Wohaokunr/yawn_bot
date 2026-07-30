import openai
from openai import OpenAI

client = OpenAI(
    base_url="https://token-plan-cn.xiaomimimo.com/v1",
    api_key="tp-cnvbscmew57f45m23dmnuet028kppbp4g1vxurun9jsdv9qs",
    # api_key="sk-sp-H.YPXXD.XpPw.MEYCIQCTdtsgJbKd3zBlYtQMLTGaemriX0ZzJlESeBE5gJerbgIhALcvZRfcpb0rigQanCe5XWWVyKfDbZVFUfzZotvOUmUj",
    # base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)





first = client.completions.create(
    # model="qwen3.8-max-preview",
    model="mimo-v2.5-pro",
    messages=[
        {
            "role":"user",
            "content":"你好"
        }
    ]
)













# first = client.responses.create(
#     # model="qwen3.8-max-preview",
#     model="mimo-v2.5-pro",
#     input="记住：我喜欢 Python 和 Rust。",
#     # store=True,
# )

# print("第一轮回答：", first.output_text)

# second = client.responses.create(
#     # model="qwen3.8-max-preview",
#     model="mimo-v2.5-pro",
#     previous_response_id=first.id,
#     input="我喜欢什么？",
#     # store=True,
# )

# print("第二轮回答：", second.output_text)

#sk-sp-H.YPXXD.XpPw.MEYCIQCTdtsgJbKd3zBlYtQMLTGaemriX0ZzJlESeBE5gJerbgIhALcvZRfcpb0rigQanCe5XWWVyKfDbZVFUfzZotvOUmUj model="qwen3.8-max-preview",