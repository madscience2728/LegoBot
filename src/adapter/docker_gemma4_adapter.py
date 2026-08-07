'''
Docker Gemma 4 Adapter - Interface for Gemma 4 running via llama.cpp server in Docker

Configuration is loaded from config/config.json (server/runtime settings)
and config/model_paths.json (where the actual .gguf files live on disk).
The Docker run command is generated automatically based on both.
If the server is not running, the adapter will print the correct command and exit.

Requirements:
- NVIDIA GPU with CUDA support
- Model files at the location specified in config/model_paths.json
- Docker with GPU support enabled
'''

import asyncio
import json
import httpx
import requests
from pathlib import Path

from src.node_result import NodeResult
from src.tools.token_guard import trim_messages_to_fit

_CONFIG_DIR = Path(__file__).parent.parent.parent / 'config'


def load_config():
    """Load config from config/config.json"""
    config_path = _CONFIG_DIR / 'config.json'
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_model_paths():
    """Load config/model_paths.json -- the on-disk location of the .gguf
    files, kept separate from config.json since it's machine-specific
    (a different path on every dev's machine) rather than a runtime
    tuning knob. Returns None if the file doesn't exist, so callers can
    fall back to the old ${PWD}-relative behavior."""
    model_paths_file = _CONFIG_DIR / 'model_paths.json'
    if not model_paths_file.exists():
        return None
    with open(model_paths_file, 'r', encoding='utf-8') as f:
        return json.load(f)


# Load config at module level for MAX_PARALLEL_PREDICTIONS (used by brain.py)
_config = load_config()
MAX_PARALLEL_PREDICTIONS = _config['lm_config']['max_parallel_predictions']


class DockerGemma4Adapter:
    def __init__(self, enable_thinking=False):
        """
        Initialize Docker Gemma 4 Adapter

        Args:
            enable_thinking (bool): Enable model thinking mode.
                                   False for normal/fast adapter, True for heavy adapter.
        """
        # Load config
        config = load_config()
        lm_config = config['lm_config']

        # Configuration from centralized config
        self.enable_thinking = enable_thinking
        self.base_model = lm_config['model_name']
        self.base_url = lm_config['base_url']
        self.completions_url = f"{self.base_url}/v1/chat/completions"
        self.base_httpx_timeout = httpx.Timeout(lm_config['httpx_timeout'], connect=30.0)
        self.tokens_count = lm_config['context_window']
        self.max_parallel_predictions = lm_config['max_parallel_predictions']
        self.mmproj = lm_config.get('mmproj', None)
        # Kept for token_guard.trim_messages_to_fit -- avoids re-reading
        # config.json off disk on every single completion call.
        self._lm_config = lm_config

        # Initialization messages
        thinking_status = "ON" if self.enable_thinking else "OFF"
        print(f"[Docker Gemma 4 Adapter initializing with thinking mode: {thinking_status}]")
        print(f"NOTE: Ensure Docker container is running on port {self.base_url.split(':')[-1]}")
        print("NOTE: Ensure GPU support is enabled in Docker")
        print("NOTE: Ensure flash attention is enabled (--flash-attn on)")
        print(f"NOTE: Ensure context window is {self.tokens_count}")
        print(f"NOTE: Ensure max parallel predictions is {self.max_parallel_predictions}")
        print(f"NOTE: Ensure model is {self.base_model}")
        if self.mmproj:
            print(f"NOTE: Ensure mmproj is {self.mmproj}")

        print("Creating async HTTP client...")
        self.client = httpx.AsyncClient(timeout=self.base_httpx_timeout)

        print("[Adapter ready for loading]")

    def _assemble_docker_command(self):
        """Assemble the Docker run command from config.

        Volume mount source: config/model_paths.json's "model_dir" if
        present, otherwise falls back to ${PWD} (the old behavior, for
        anyone who's just running docker from inside the model folder
        directly rather than pointing at it explicitly).
        """
        config = load_config()
        lm_config = config['lm_config']
        model_paths = load_model_paths()

        # Extract base URL port
        port = lm_config['base_url'].split(':')[-1]

        if model_paths and model_paths.get('model_dir'):
            mount_source = model_paths['model_dir']
            model_filename = model_paths.get('model_file', lm_config['model_name'])
            mmproj_filename = model_paths.get('mmproj_file', lm_config.get('mmproj'))
        else:
            mount_source = '${PWD}'
            model_filename = lm_config['model_name']
            mmproj_filename = lm_config.get('mmproj')

        cmd_parts = [
            'docker run --rm --gpus all',
            f'-p {port}:{port}',
            f'-v "{mount_source}:/models"',
            'ghcr.io/ggml-org/llama.cpp:server-cuda',
            f'--model /models/{model_filename}'
        ]

        # Add mmproj if configured
        if mmproj_filename:
            cmd_parts.append(f'--mmproj /models/{mmproj_filename}')

        # Add remaining flags
        cmd_parts.extend([
            '--host 0.0.0.0',
            f'--port {port}',
            '--n-gpu-layers 99',
            f'--ctx-size {lm_config["context_window"]}',
            f'--parallel {lm_config["max_parallel_predictions"]}',
            '--flash-attn on',
            '--jinja',
            '--no-cache-prompt',
            f'--temp {lm_config.get("temperature", 0.95)}',
            f'--repeat-penalty {lm_config.get("repeat_penalty", 1.15)}',
            f'--repeat-last-n {lm_config.get("repeat_last_n", 128)}'
        ])

        return ' '.join(cmd_parts)

    async def load(self):
        """Async initialization - performs health check"""
        print("Testing connection to Docker Gemma 4 server...")
        try:
            response = await self.client.get(f"{self.base_url}/v1/models")
            if response.status_code != 200:
                raise Exception(f"Expected status code 200, got {response.status_code}")
            print("[Docker Gemma 4 Adapter loaded successfully]")
        except Exception as e:
            print(f"⚠️  Failed to connect to Docker Gemma 4 server: {e}")
            print("\n" + "=" * 80)
            print("Docker server is not running. Please start it with this command:")
            print("=" * 80)
            print(f"\n{self._assemble_docker_command()}\n")
            print("=" * 80)
            raise SystemExit("Docker server not running. Exiting gracefully.")

    def complete_synchronous(self, messages):
        """Synchronous completion call"""
        payload = {
            "model": self.base_model,
            "messages": messages,
            "max_tokens": 8192,
            "t_max_predict_ms": 5200,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking}
        }
        response = requests.post(
            self.completions_url,
            json=payload
        )
        try:
            rj = response.json()
            result = NodeResult(
                content=rj["choices"][0]["message"]["content"],
                prompt_tokens=rj["usage"]["prompt_tokens"],
                completion_tokens=rj["usage"]["completion_tokens"]
            )
            return result
        except Exception as e:
            raise Exception("Failed to parse response!") from e

    async def complete_async(self, messages):
        """Async completion call with error handling.

        Trims a COPY of `messages` to fit the context window before
        sending -- see token_guard.trim_messages_to_fit's docstring for
        why this exists as a backstop separate from LMNode's own
        history trimming. The caller's own list/history is never
        mutated by this.
        """
        messages = await trim_messages_to_fit(messages, lm_config=self._lm_config)
        payload = {
            "model": self.base_model,
            "messages": messages,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking}
        }
        response = None
        try:
            response = await self.client.post(
                self.completions_url,
                json=payload
            )
            rj = response.json()
            result = NodeResult(
                content=rj["choices"][0]["message"]["content"],
                prompt_tokens=rj["usage"]["prompt_tokens"],
                completion_tokens=rj["usage"]["completion_tokens"]
            )
            return result
        except httpx.ReadTimeout:
            print("⚠️ Request timeout - Docker Gemma 4 didn't respond in time, continuing...")
            return NodeResult(content="", prompt_tokens=0, completion_tokens=0)
        except httpx.HTTPError as e:
            print(f"⚠️ HTTP error occurred: {e}, continuing...")
            return NodeResult(content="", prompt_tokens=0, completion_tokens=0)
        except Exception as e:
            print(f"⚠️ Failed to parse response: {e}, this is what we DO know...")
            if response is not None:
                try:
                    print(f"{json.dumps(response.json(), indent=2)}")
                except Exception:
                    print(f"(response body wasn't JSON either: {response.text[:500]!r})")
            return NodeResult(content="", prompt_tokens=0, completion_tokens=0)

    async def complete_async_with_tools(self, messages, tools):
        """
        Complete with tool calling support.
        Returns dict with either 'content' or 'tool_calls' key.

        Same trim-before-send backstop as complete_async -- this path
        (lm_tool_node.py) builds its own message list and was one of
        the callers NOT covered by LMNode's preprocess()-time trimming.
        """
        messages = await trim_messages_to_fit(messages, lm_config=self._lm_config)
        payload = {
            "model": self.base_model,
            "messages": messages,
            "tools": tools,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking}
        }
        try:
            response = await self.client.post(
                self.completions_url,
                json=payload
            )
            response_json = response.json()
            message = response_json["choices"][0]["message"]

            # Check if model requested tool calls
            if "tool_calls" in message and message["tool_calls"]:
                return {
                    "tool_calls": message["tool_calls"],
                    "content": message.get("content", "")
                }
            else:
                # No tool calls, return content
                return {
                    "content": message.get("content", ""),
                    "tool_calls": None
                }
        except httpx.ReadTimeout:
            print("⚠️  Request timeout (with tools) - Docker Gemma 4 didn't respond in time, continuing...")
            return {"content": "", "tool_calls": None}
        except httpx.HTTPError as e:
            print(f"⚠️  HTTP error occurred (with tools): {e}, continuing...")
            return {"content": "", "tool_calls": None}
        except Exception as e:
            print(f"⚠️  Failed to parse tool response: {e}, continuing...")
            return {"content": "", "tool_calls": None}

    async def complete_async_batch(self, messages_batch: dict) -> dict:
        """Process multiple completions in parallel"""
        names = list(messages_batch.keys())
        coros = [self.complete_async(messages_batch[name]) for name in names]
        responses = await asyncio.gather(*coros, return_exceptions=True)
        # Handle any exceptions that slipped through
        results = []
        for i, response in enumerate(responses):
            if isinstance(response, Exception):
                print(f"⚠️  Batch request failed for {names[i]}: {response}, continuing...")
                results.append(NodeResult(content="", prompt_tokens=0, completion_tokens=0))
            else:
                results.append(response)
        return dict(zip(names, results))

    def test_health(self):
        """Synchronous health check"""
        response = requests.get(f"{self.base_url}/v1/models")
        if response.status_code != 200:
            raise Exception(f"Expected status code 200, got {response.status_code}")
