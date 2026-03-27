import os
import requests
from typing import Optional, Any
from config import (
	API_KEY, BASE_URL, NEURAL_PROVIDER, NEURAL_MODEL, NEURAL_TIMEOUT,
	OLLAMA_BASE_URL, OLLAMA_MODEL,
)


def _iter_sse_lines(resp: requests.Response):
	for raw in resp.iter_lines(decode_unicode=True):
		if not raw:
			continue
		line = raw.strip()
		if not line:
			continue
		yield line


class NeuralNetworkManager:
	def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, provider: Optional[str] = None, model: Optional[str] = None, timeout: Optional[int] = None):
		self.api_key = api_key or API_KEY
		self.base_url = (base_url or BASE_URL) or None
		self.provider = (provider or NEURAL_PROVIDER or "openai").lower()
		if self.provider == "ollama":
			self.model = model or OLLAMA_MODEL
			self.base_url = (base_url or OLLAMA_BASE_URL) or None
			# Ollama: longer timeout (model load + first reply can be 60+ sec)
			self.timeout = timeout or int(os.getenv("OLLAMA_TIMEOUT", "120"))
		else:
			self.model = model or NEURAL_MODEL
		if self.provider != "ollama":
			# DeepSeek и др. могут долго «думать» — не меньше 120 сек для API (Read timed out)
			t = timeout if timeout is not None else NEURAL_TIMEOUT
			if self.provider == "deepseek" and t < 60:
				t = 120
			self.timeout = t

	def _anthropic_post_messages(self, payload: dict[str, Any]) -> dict:
		if not self.api_key or not self.base_url:
			raise RuntimeError("Anthropic API key or base URL not configured")
		headers = {
			"x-api-key": self.api_key,
			"anthropic-version": "2023-06-01",
			"Content-Type": "application/json",
		}
		url = f"{self.base_url.rstrip('/')}/v1/messages"
		resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
		try:
			resp.raise_for_status()
		except requests.HTTPError:
			raise RuntimeError(f"Anthropic API error {resp.status_code}: {resp.text}")
		return resp.json()

	def _openai_post_chat(self, payload: dict[str, Any]) -> dict:
		if not self.api_key or not self.base_url:
			raise RuntimeError("OpenAI API key or base URL not configured")
		headers = {
			"Authorization": f"Bearer {self.api_key}",
			"Content-Type": "application/json",
		}
		url = f"{self.base_url.rstrip('/')}/v1/chat/completions"
		resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
		try:
			resp.raise_for_status()
		except requests.HTTPError:
			raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text}")
		return resp.json()

	def _openai_post_chat_stream(self, payload: dict[str, Any]):
		if not self.api_key or not self.base_url:
			raise RuntimeError("OpenAI API key or base URL not configured")
		headers = {
			"Authorization": f"Bearer {self.api_key}",
			"Content-Type": "application/json",
			"Accept": "text/event-stream",
		}
		url = f"{self.base_url.rstrip('/')}/v1/chat/completions"
		p = dict(payload or {})
		p["stream"] = True
		resp = requests.post(url, json=p, headers=headers, timeout=self.timeout, stream=True)
		try:
			resp.raise_for_status()
		except requests.HTTPError:
			raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text}")
		for line in _iter_sse_lines(resp):
			if not line.startswith("data:"):
				continue
			data = line[len("data:") :].strip()
			if not data:
				continue
			if data == "[DONE]":
				break
			try:
				chunk = requests.utils.json.loads(data)
			except Exception:
				try:
					import json as _json
					chunk = _json.loads(data)
				except Exception:
					continue
			if not isinstance(chunk, dict):
				continue
			choices = chunk.get("choices")
			if not isinstance(choices, list) or not choices:
				continue
			d0 = choices[0]
			if not isinstance(d0, dict):
				continue
			delta = d0.get("delta")
			if isinstance(delta, dict):
				c = delta.get("content")
				if isinstance(c, str) and c:
					yield c
			continue

		try:
			resp.close()
		except Exception:
			pass

	def _ollama_chat_stream(self, messages: list[dict[str, Any]], system: Optional[str] = None, max_tokens: int = 300):
		"""Стриминг чата через Ollama API (локально, бесплатно)."""
		base = (self.base_url or OLLAMA_BASE_URL).rstrip("/")
		model = (self.model or OLLAMA_MODEL or "").strip() or "qwen2.5:14b-instruct"
		payload = {
			"model": model,
			"messages": messages,
			"stream": True,
			"options": {
				"num_predict": max(1, min(max_tokens, 8192)),
				"num_ctx": 4096,
				"num_thread": 8,
				"temperature": 0.7
			},
		}
		if system:
			payload["messages"] = [{"role": "system", "content": system}] + list(payload["messages"])
		try:
			resp = requests.post(
				f"{base}/api/chat",
				json=payload,
				timeout=self.timeout,
				stream=True,
			)
			resp.raise_for_status()
		except requests.RequestException as e:
			err_text = ""
			if hasattr(e, "response") and e.response is not None:
				try:
					err_text = (e.response.text or "")[:500]
				except Exception:
					pass
			hint = f" Модель: {model}. Запусти Ollama и выбери в JARVIS: Ollama — Qwen 2.5 32B."
			if err_text:
				hint += f" Ответ сервера: {err_text}"
			if getattr(e, "response", None) is not None and getattr(e.response, "status_code", None) == 404:
				if "not found" in (err_text or "").lower():
					hint += f" Скачать модель: ollama pull {model}"
			yield f"Ошибка Ollama: {e}.{hint}"
			return
		try:
			for line in resp.iter_lines(decode_unicode=True):
				if not line:
					continue
				try:
					chunk = requests.utils.json.loads(line)
				except Exception:
					continue
				if isinstance(chunk, dict):
					msg = chunk.get("message") or {}
					if isinstance(msg, dict):
						c = msg.get("content")
						if isinstance(c, str) and c:
							yield c
		except (requests.RequestException, ConnectionError, OSError) as e:
			err_text = ""
			if hasattr(e, "response") and getattr(e, "response", None) is not None:
				try:
					err_text = (getattr(e.response, "text", "") or "")[:300]
				except Exception:
					pass
			yield f"Ошибка соединения Ollama: {e}.{f' Ответ: {err_text}' if err_text else ''}"
		finally:
			try:
				resp.close()
			except Exception:
				pass

	def _ollama_chat(self, messages: list[dict[str, Any]], system: Optional[str] = None, max_tokens: int = 300) -> str:
		"""Один ответ без стрима через Ollama API."""
		base = (self.base_url or OLLAMA_BASE_URL).rstrip("/")
		model = (self.model or OLLAMA_MODEL or "").strip() or "qwen2.5:14b-instruct"
		payload = {"model": model, "messages": messages, "stream": False}
		if system:
			payload["messages"] = [{"role": "system", "content": system}] + list(payload["messages"])
		try:
			resp = requests.post(f"{base}/api/chat", json=payload, timeout=self.timeout)
			resp.raise_for_status()
			data = resp.json()
		except requests.RequestException as e:
			err_text = ""
			if hasattr(e, "response") and e.response is not None:
				try:
					err_text = (e.response.text or "")[:500]
				except Exception:
					pass
			hint = f" Модель: {model}. Убедись, что Ollama запущен (в трее или ollama list) и в JARVIS выбран Ollama — Qwen 2.5 32B."
			if err_text:
				hint += f" Ответ сервера: {err_text}"
			# If model not found, suggest pull command
			if getattr(e, "response", None) is not None and getattr(e.response, "status_code", None) == 404:
				if "not found" in (err_text or "").lower():
					hint += f" Скачать модель: ollama pull {model}"
			return f"Ошибка Ollama: {e}.{hint}"
		msg = (data or {}).get("message") or {}
		return (msg.get("content") or "").strip()

	def _call_anthropic(self, prompt: str, max_tokens: int = 150):
		if not self.api_key or not self.base_url:
			raise RuntimeError("Anthropic API key or base URL not configured")
		model_candidates = []
		if self.model:
			model_candidates.append(self.model)
		for m in (
			"claude-3-5-haiku-20241022",
			"claude-sonnet-4-5-20250929",
			"claude-sonnet-4-5",
		):
			if m not in model_candidates:
				model_candidates.append(m)

		last_model_err = None
		for model in model_candidates:
			payload = {
				"model": model,
				"max_tokens": max_tokens,
				"messages": [
					{"role": "user", "content": prompt}
				],
			}
			try:
				return self._anthropic_post_messages(payload)
			except RuntimeError as e:
				# Try next model only if this is a model not_found
				msg = str(e)
				if "not_found_error" in msg and "model" in msg:
					last_model_err = f"{model}: {msg}"
					continue
				raise

		raise RuntimeError(f"Anthropic model not found. Tried: {', '.join(model_candidates)}. Last: {last_model_err}")

	def generate_chat_response(self, messages: list[dict[str, Any]], system: Optional[str] = None, max_tokens: int = 300) -> str:
		"""Multi-turn chat. messages is a list of {role: user|assistant, content: str}."""
		try:
			if self.provider == "ollama":
				return self._ollama_chat(messages, system=system, max_tokens=max_tokens)
			if self.provider == "anthropic":
				model_candidates = []
				if self.model:
					model_candidates.append(self.model)
				for m in (
					"claude-3-5-haiku-20241022",
					"claude-sonnet-4-5-20250929",
					"claude-sonnet-4-5",
				):
					if m not in model_candidates:
						model_candidates.append(m)

				last_model_err = None
				for model in model_candidates:
					payload: dict[str, Any] = {
						"model": model,
						"max_tokens": max_tokens,
						"messages": messages,
					}
					if system:
						payload["system"] = system
					try:
						result = self._anthropic_post_messages(payload)
						break
					except RuntimeError as e:
						msg = str(e)
						if "not_found_error" in msg and "model" in msg:
							last_model_err = f"{model}: {msg}"
							continue
						raise
				else:
					raise RuntimeError(f"Anthropic model not found. Tried: {', '.join(model_candidates)}. Last: {last_model_err}")
			else:
				payload = {
					"model": self.model or ("deepseek-chat" if self.provider == "deepseek" else "gpt-3.5-turbo"),
					"messages": ([{"role": "system", "content": system}] if system else []) + messages,
					"max_tokens": max_tokens,
				}
				result = self._openai_post_chat(payload)
		except Exception as e:
			return f"Ошибка при обращении к нейросети: {e}"

		# Anthropic Messages API
		if isinstance(result, dict) and "content" in result and isinstance(result["content"], list):
			texts = []
			for block in result["content"]:
				if isinstance(block, dict) and block.get("type") == "text":
					texts.append(block.get("text", ""))
			return "".join(texts).strip()

		# OpenAI chat
		if isinstance(result, dict) and "choices" in result and isinstance(result["choices"], list) and result["choices"]:
			msg = result["choices"][0].get("message", {})
			if isinstance(msg, dict):
				return (msg.get("content") or "").strip()

		return str(result)

	def generate_chat_response_stream(self, messages: list[dict[str, Any]], system: Optional[str] = None, max_tokens: int = 300):
		try:
			if self.provider == "ollama":
				for c in self._ollama_chat_stream(messages, system=system, max_tokens=max_tokens):
					yield c
				return
			if self.provider == "anthropic":
				yield self.generate_chat_response(messages, system=system, max_tokens=max_tokens)
				return
			payload = {
				"model": self.model or ("deepseek-chat" if self.provider == "deepseek" else "gpt-3.5-turbo"),
				"messages": ([{"role": "system", "content": system}] if system else []) + messages,
				"max_tokens": max_tokens,
			}
			for c in self._openai_post_chat_stream(payload):
				yield c
		except Exception as e:
			yield f"Ошибка при обращении к нейросети: {e}"

	def generate_response(self, prompt: str, max_tokens: int = 150) -> str:
		try:
			if self.provider == "anthropic":
				result = self._call_anthropic(prompt, max_tokens=max_tokens)
			else:
				# OpenAI-compatible providers (including DeepSeek) should use chat completions
				payload = {
					"model": self.model or ("deepseek-chat" if self.provider == "deepseek" else "gpt-3.5-turbo"),
					"messages": [{"role": "user", "content": prompt}],
					"max_tokens": max_tokens,
				}
				result = self._openai_post_chat(payload)
		except Exception as e:
			return f"Ошибка при обращении к нейросети: {e}"

		# Anthropic Messages API
		if isinstance(result, dict) and "content" in result and isinstance(result["content"], list):
			texts = []
			for block in result["content"]:
				if isinstance(block, dict) and block.get("type") == "text":
					texts.append(block.get("text", ""))
			return "".join(texts).strip()

		# OpenAI-style
		if isinstance(result, dict) and "choices" in result and isinstance(result["choices"], list) and result["choices"]:
			text = result["choices"][0].get("text") or result["choices"][0].get("message", {}).get("content")
			return (text or "").strip()

		return str(result)


