#!/usr/bin/env python3
"""
Sliding Window RAW TEXT Parser
Extracts complete contextual thought structures from large texts
using overlapping windows to preserve reasoning chains.
Optimized for GPT-4.1 via SecureChatAI and designed for training pipeline integration.
"""

import re
import json
import argparse
import os
import time
from typing import List, Tuple
from dataclasses import dataclass
import tiktoken
from rag_pipeline.processing.ai_client import chat_completion, DEFAULT_MODEL
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.storage.storage import StorageManager

logger = setup_logger()


# Strict output rules for extraction - prepended to system prompt
STRICT_OUTPUT_RULES = """CRITICAL OUTPUT RULES:
- Output ONLY the extracted content
- Do NOT include reasoning, analysis, explanations, or meta commentary
- Do NOT include <think>, <analysis>, or similar markers
- Do NOT explain what you are doing
- If content is already clean, return it verbatim
- If you violate these rules, the output is invalid
---

"""

# Default system prompt for extraction
DEFAULT_SYSTEM_PROMPT = STRICT_OUTPUT_RULES + """You are a content extraction assistant. Your job is to extract the main, relevant content from the provided text while removing any navigation, boilerplate, or irrelevant elements. Output ONLY the extracted content. Preserve important information like dates, names, numbers, and structured data (tables). If the content is already clean, return it as-is without modification."""

# Default user prompt template
DEFAULT_USER_TEMPLATE = """Extract the main content from this text.

REMOVE: navigation, headers, footers, menus, scripts, boilerplate
PRESERVE: tables, lists, dates, names, numbers, factual wording

Do not summarize or rewrite. Preserve the original factual wording.

--- BEGIN TEXT ---
{window_text}
--- END TEXT ---"""

@dataclass
class ProcessingStats:
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0
    windows_processed: int = 0
    concepts_extracted: int = 0
    start_time: float = 0.0


class SlidingWindowParser:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        window_size: int = 25000,
        overlap: int = 8000,
    ):
        self.model = model
        self.window_size = window_size
        self.overlap = overlap

        try:
            self.tokenizer = tiktoken.encoding_for_model("gpt-4")
        except Exception:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        self.pricing = {
            "input_standard": 0.27,
            "input_discount": 0.135,
            "output_standard": 1.10,
            "output_discount": 0.55,
        }
        self.stats = ProcessingStats()

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text))

    def split_into_sections(self, text: str):
        # Split on each Section Number: ###
        parts = re.split(r'(?=Section Number:\s*\d+)', text)
        return [p.strip() for p in parts if p.strip()]

    def create_windows(self, text: str) -> List[Tuple[str, int, int]]:
        print(f"🔍 Creating sliding windows...")
        print(f"   Window size: {self.window_size:,} tokens")
        print(f"   Overlap: {self.overlap:,} tokens")
        tokens = self.tokenizer.encode(text)
        total_tokens = len(tokens)
        self.stats.total_tokens = total_tokens
        print(f"   Total tokens: {total_tokens:,}")

        windows = []
        start = 0
        while start < total_tokens:
            end = min(start + self.window_size, total_tokens)
            window_tokens = tokens[start:end]
            window_text = self.tokenizer.decode(window_tokens)
            windows.append((window_text, start, end))
            if end >= total_tokens:
                break
            start = end - self.overlap
        print(f"   Created {len(windows)} windows")
        return windows

    def _sanitize_ai_output(self, text: str, fallback_text: str) -> str:
        """
        Defensive post-processing to clean AI output.

        - Strips <think>...</think> blocks
        - Strips leading conversational phrases
        - Trims whitespace
        - Falls back to cleaned raw text if empty
        """
        if not text:
            return fallback_text.strip()

        result = text

        # Strip <think>...</think> blocks (case-insensitive, multiline)
        result = re.sub(r'<think>.*?</think>', '', result, flags=re.IGNORECASE | re.DOTALL)

        # Strip <analysis>...</analysis> blocks
        result = re.sub(r'<analysis>.*?</analysis>', '', result, flags=re.IGNORECASE | re.DOTALL)

        # Strip leading conversational phrases
        leading_phrases = [
            r'^Okay,?\s*',
            r'^Sure,?\s*',
            r'^Here is the extracted content:?\s*',
            r'^Here\'s the extracted content:?\s*',
            r'^The extracted content is:?\s*',
            r'^Extracted content:?\s*',
        ]
        for pattern in leading_phrases:
            result = re.sub(pattern, '', result, flags=re.IGNORECASE)

        result = result.strip()

        # If result is empty after sanitization, fall back to cleaned raw text
        if not result:
            logger.warning("AI output was empty after sanitization, using fallback")
            return fallback_text.strip()

        return result

    def extract_from_window(self, window_text: str, thinker_name: str, window_num: int, total_windows: int) -> List[str]:
        """
        Process a sliding window through AI to extract clean, RAG-ready content.
        Always uses AI - the whole point of this tool.

        Returns list of extracted text sections (usually one per window).
        """
        # Load prompts from config or use sensible defaults
        system_prompt, user_prompt = self._load_prompts(window_text)
        fallback_used = False

        try:
            logger.info(f"Window {window_num}/{total_windows}: Calling AI model={self.model}")
            print(f"   Processing window {window_num}/{total_windows} via AI (model={self.model})...")

            raw_response = chat_completion(
                user_prompt,
                model_hint=self.model,
                temperature=0.1,  # Low temp for faithful extraction
                max_tokens=4000,  # Allow longer responses for full content
                system_prompt=system_prompt,
            )

            self.stats.input_tokens += self.count_tokens(user_prompt)
            self.stats.output_tokens += self.count_tokens(raw_response)

            # Sanitize AI output
            clean_text = self._sanitize_ai_output(raw_response, window_text)

            if clean_text == window_text.strip():
                fallback_used = True
                logger.warning(f"Window {window_num}: Used fallback (AI returned empty/invalid)")

            logger.info(f"Window {window_num}: Extracted {len(clean_text)} chars, fallback={fallback_used}")
            print(f"      → Extracted {len(clean_text)} chars")
            return [clean_text]

        except Exception as e:
            fallback_used = True
            logger.error(f"Window {window_num}: AI extraction failed: {e}, using fallback")
            print(f"   ⚠️ AI extraction failed for window {window_num}: {e}")
            print(f"      → Falling back to raw text")
            return [window_text.strip()]

    def _load_prompts(self, window_text: str) -> tuple[str, str]:
        """Load prompts from config file or return sensible defaults."""
        config_path = "config/sliding_window_prompts.json"

        system_prompt = DEFAULT_SYSTEM_PROMPT
        user_template = DEFAULT_USER_TEMPLATE

        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)

                loaded_system = cfg.get("system", "").strip()
                loaded_user = cfg.get("user_template", "").strip()

                # Use config values if they look valid (not corrupted)
                # Always prepend strict output rules to system prompt
                if loaded_system and "Ã" not in loaded_system:
                    # Ensure strict rules are at the top
                    if "CRITICAL OUTPUT RULES" not in loaded_system:
                        system_prompt = STRICT_OUTPUT_RULES + loaded_system
                    else:
                        system_prompt = loaded_system

                if loaded_user and "Ã" not in loaded_user and "{window_text}" in loaded_user:
                    user_template = loaded_user

            except Exception as e:
                logger.warning(f"Failed to load prompts from config: {e}")

        user_prompt = user_template.format(window_text=window_text)
        return system_prompt, user_prompt


    def _parse_extracts(self, response: str) -> List[str]:
        extracts = []
        lines = response.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("EXTRACT:"):
                extract = line[8:].strip()
                if len(extract) > 30:
                    extract = re.sub(r"\s+", " ", extract)
                    extracts.append(extract)

        if not extracts:
            for line in lines:
                line = line.strip()
                if line and len(line) > 30 and not line.lower().startswith(
                    ("note:", "commentary:", "translator:", "editor:")
                ):
                    line = re.sub(r"^\d+\.\s*", "", line)
                    line = re.sub(r"^[-*•]\s*", "", line)
                    line = re.sub(r'^[\"\u201c]|[\"\u201d]$', "", line)
                    line = re.sub(r"\s+", " ", line).strip()
                    if line:
                        extracts.append(line)

        return extracts

    def calculate_cost(self) -> float:
        input_cost = (self.stats.input_tokens / 1_000_000) * self.pricing["input_discount"]
        output_cost = (self.stats.output_tokens / 1_000_000) * self.pricing["output_discount"]
        self.stats.total_cost = input_cost + output_cost
        return self.stats.total_cost

    def calculate_cost_estimates(self):
        """Logs cost estimates assuming all cache miss tokens for standard and discount pricing."""

        input_million_tokens = self.stats.input_tokens / 1_000_000
        output_million_tokens = self.stats.output_tokens / 1_000_000

        regular_cost = (input_million_tokens * 0.55) + (output_million_tokens * 2.19)  # 100% cache miss, standard rate
        discount_cost = (input_million_tokens * 0.135) + (output_million_tokens * 0.55)  # 100% cache miss, discount rate

        print(f"\n💰 Cost estimates assuming 100% cache misses:")
        print(f"     Regular hours cost: ${regular_cost:.6f}")
        print(f"     Discount hours cost: ${discount_cost:.6f}")

    def deduplicate_extracts(self, extracts: List[str]) -> List[str]:
        seen = set()
        unique_extracts = []
        for extract in extracts:
            normalized = re.sub(r'\s+', ' ', extract.lower().strip())
            if normalized not in seen and len(extract) >= 30:
                seen.add(normalized)
                unique_extracts.append(extract)
        return unique_extracts

    def _rag_ready_path(self, output_file: str) -> str:
        # Always write JSONL into cache/rag_ready/<basename>.jsonl
        base = os.path.basename(output_file)
        path = os.path.join("cache", "rag_ready", base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def save_section_json(self, section_json_str, output_file):
        """Save ONE section as a single JSON object for RAG ingestion."""
        final_path = self._rag_ready_path(output_file)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        # Ensure the extract is valid JSON
        section_obj = json.loads(section_json_str)

        with open(final_path, "w", encoding="utf-8") as f:
            json.dump(section_obj, f, ensure_ascii=False, indent=2)

        logger.info(f"Saved JSON for ingestion: {final_path}")

    def save_to_jsonl(self, extracts, output_file, source="main", section_id=None, url=None):
        final_path = self._rag_ready_path(output_file)
        with open(final_path, "w", encoding="utf-8") as f:
            for extract in extracts:
                record = {
                    "text": extract,
                    "metadata": {
                        "source": source,
                        "section_id": section_id or "",
                        "url": url or ""
                    }
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(f"Saved JSONL locally: {final_path}")

    def process_file(self, input_file: str, output_file: str, thinker_name: str) -> tuple[int, list[dict]]:
        self.stats.start_time = time.time()

        print(f"🚀 Sliding Window Processing")
        print(f"📁 Input: {input_file}")
        print(f"🎯 Output: {output_file}")
        print(f"🧠 Thinker: {thinker_name}")
        print(f"🤖 Model: {self.model}")
        print("=" * 60)

        with open(input_file, 'r', encoding='utf-8') as f:
            text = f.read()

        # 🧩 Doc-specific overfit mode: split by explicit "Section Number"
        if "All Content" in os.path.basename(input_file):
            print("🧠 Detected 'All Content' document — switching to section-by-section extraction mode")
            try:
                sections = self.split_into_sections(text)
                print(f"   Found {len(sections)} discrete sections")

                all_extracts = []
                for idx, section_text in enumerate(sections, 1):
                    section_id = re.search(r'Section Number:\s*(\d+)', section_text)
                    section_id = section_id.group(1) if section_id else f"{idx:03d}"
                    print(f"   → Extracting Section {section_id}")

                    extracts = self.extract_from_window(section_text, thinker_name, idx, len(sections))
                    if extracts:
                        all_extracts.extend(extracts)
                print("✅ Section-based extraction complete.")
                # Build sections data for canonical JSON output
                sections_data = [
                    {
                        "text": extract,
                        "window_index": idx,
                        "char_start": None,
                        "char_end": None,
                        "section_title": None,
                        "ai_normalized": True,
                        "ai_trigger_reason": "always_ai",
                        "ai_request_count": 1,
                    }
                    for idx, extract in enumerate(all_extracts, start=1)
                ]
                return len(all_extracts), sections_data
            except Exception as e:
                print(f"⚠️ Section split failed, reverting to normal sliding-window mode ({e})")

        windows = self.create_windows(text)

        print(f"\n🔄 Processing {len(windows)} windows...")
        all_extracts = []

        for i, (window_text, start_pos, end_pos) in enumerate(windows, 1):
            extracts = self.extract_from_window(window_text, thinker_name, i, len(windows))
            all_extracts.extend(extracts)
            self.stats.windows_processed += 1

            if i % 5 == 0 or i == len(windows):
                elapsed = time.time() - self.stats.start_time
                rate = i / elapsed * 60
                eta = (len(windows) - i) / rate if rate > 0 else 0
                print(f"   Progress: {i}/{len(windows)} windows ({i/len(windows)*100:.1f}%) | Rate: {rate:.1f} windows/min | ETA: {eta:.1f} min")

        print(f"\n🔍 Deduplicating extracts...")
        print(f"   Before: {len(all_extracts)} extracts")
        unique_extracts = self.deduplicate_extracts(all_extracts)
        print(f"   After: {len(unique_extracts)} unique extracts")

        total_time = time.time() - self.stats.start_time
        cost = self.calculate_cost()
        self.stats.concepts_extracted = len(unique_extracts)

        self.calculate_cost_estimates()

        print("=" * 60)
        print(f"✅ Processing Complete!")
        print(f"⏱️  Total time: {total_time / 60:.1f} minutes")
        print(f"🪟 Windows processed: {self.stats.windows_processed}")
        print(f"📊 Input tokens: {self.stats.input_tokens:,}")
        print(f"📤 Output tokens: {self.stats.output_tokens:,}")
        print(f"💰 Estimated cost (discount pricing): ${cost:.3f}")
        print(f"🧠 Thought structures extracted: {len(unique_extracts)}")
        print(f"🎯 Ready for canonical JSON output!")

        # Build sections data for canonical JSON output
        sections_data = [
            {
                "text": extract,
                "window_index": idx,
                "char_start": None,
                "char_end": None,
                "section_title": None,
                "ai_normalized": True,  # Always AI in this version
                "ai_trigger_reason": "always_ai",
                "ai_request_count": 1,
            }
            for idx, extract in enumerate(unique_extracts, start=1)
        ]

        return len(unique_extracts), sections_data


def main():
    parser = argparse.ArgumentParser(
        description="Sliding Window Raw Text Parser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m rag_pipeline.processing.sliding_window input.txt output.jsonl --thinker "IRB Policy"
  python -m rag_pipeline.processing.sliding_window corpus.txt output.jsonl --thinker "Stanford IRB" --window-size 20000 --overlap 5000
        """,
    )
    parser.add_argument("input_file", help="Input text file")
    parser.add_argument("output_file", help="Output JSONL file")
    parser.add_argument("--thinker", required=True, help="Context label for extracts (e.g. 'IRB Policy')")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"AI model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--window-size", type=int, default=25000, help="Window size in tokens")
    parser.add_argument("--overlap", type=int, default=8000, help="Overlap size in tokens")

    args = parser.parse_args()

    sliding_parser = SlidingWindowParser(
        model=args.model,
        window_size=args.window_size,
        overlap=args.overlap,
    )

    try:
        count, sections = sliding_parser.process_file(args.input_file, args.output_file, args.thinker)
        print(f"\n🎉 Success! Generated {count} extracts for your pipeline.")
        print(f"   (Use rag_pipeline.main or web API for canonical JSON output)")
        return 0
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return 1

if __name__ == "__main__":
    exit(main())

