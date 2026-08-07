import httpx
import json
from pathlib import Path

def load_config():
    """Load config from config/config.json"""
    config_path = Path(__file__).parent.parent.parent / 'config' / 'config.json'
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

async def count_tokens(messages):
    """
    Count tokens for a list of messages using the configured tokenizer endpoint.
    ASYNC version using httpx.

    Args:
        messages: List of message dicts with 'role' and 'content' keys

    Returns:
        int: Total token count, or -1 if tokenization fails
    """
    try:
        config = load_config()
        tokenizer_url = config['lm_config']['tokenizer_endpoint']
        # Use the same timeout as the main adapter (default to 60s if not set)
        timeout = config['lm_config'].get('httpx_timeout', 60.0)

        # Convert messages to text (simple concatenation for now)
        # TODO: Use proper chat template formatting if needed
        # Handle messages that don't have 'content' key (e.g., tool_calls messages)
        parts = []
        for msg in messages:
            if 'content' in msg:
                parts.append(f"{msg['role']}: {msg['content']}")
            elif 'tool_calls' in msg:
                parts.append(f"{msg['role']}: [tool_calls]")
            else:
                parts.append(f"{msg['role']}: [no content]")
        text = "\n".join(parts)

        # Call tokenizer endpoint asynchronously
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(tokenizer_url, json={"content": text})

            if response.status_code == 200:
                result = response.json()
                # Assuming the endpoint returns {"tokens": [...]} or similar
                # Adjust based on actual response format
                if isinstance(result, dict) and 'tokens' in result:
                    return len(result['tokens'])
                elif isinstance(result, list):
                    return len(result)
                else:
                    print(f"[WARNING] Unexpected tokenizer response format: {result}")
                    return -1
            else:
                print(f"[WARNING] Tokenizer endpoint returned status {response.status_code}")
                return -1

    except httpx.RequestError as e:
        print(f"[WARNING] Failed to connect to tokenizer: {e}")
        return -1
    except Exception as e:
        print(f"[WARNING] Error counting tokens: {e}")
        return -1

# Legacy test code (kept for manual testing)
if __name__ == "__main__":
    import asyncio
    async def test():
        messages = [{"role": "user", "content": "your text here"}]
        count = await count_tokens(messages)
        print(f"Token count: {count}")
    asyncio.run(test())
