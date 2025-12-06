import os
import openai
from google import genai
from dotenv import load_dotenv

load_dotenv()

PRIME_API_KEY = os.getenv("PRIME_API_KEY")
PRIME_TEAM_ID = os.getenv("PRIME_TEAM_ID")

client = openai.OpenAI(
    api_key=PRIME_API_KEY,
    base_url="https://api.pinference.ai/api/v1",
    default_headers={
        "X-Prime-Team-ID": PRIME_TEAM_ID
    }
)


def main():
    response = client.chat.completions.create(
        model="google/gemini-3-pro-preview",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Explain quantum computing in simple terms."}
        ],
        max_tokens=500,
        temperature=0.7
    )
    print(response)
    return response


if __name__ == "__main__":
    main()
