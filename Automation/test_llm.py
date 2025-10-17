# test_llm.py - Test if OpenAI API is working
import os
from openai import OpenAI

print("Testing OpenAI API connection...")
print(f"API Key set: {'Yes' if os.getenv('OPENAI_API_KEY') else 'No'}")
print(f"Model: {os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}")

try:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    
    print(f"\nAttempting API call with model: {model}")
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say 'API working' and nothing else."}
        ],
        max_tokens=100
    )
    
    result = response.choices[0].message.content
    print(f"\n✓ SUCCESS!")
    print(f"Response: {result}")
    print(f"Response length: {len(result)} chars")
    
except Exception as e:
    print(f"\n✗ FAILED!")
    print(f"Error type: {type(e).__name__}")
    print(f"Error message: {str(e)}")
    import traceback
    traceback.print_exc()