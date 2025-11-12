#!/usr/bin/env python3
"""
Sliding Window RAW TEXT Parser
Extracts complete contextual thought structures from large texts
using overlapping windows to preserve reasoning chains.
Optimized for DeepSeek API and designed for training pipeline integration.
"""

import re
import json
import argparse
import os
import time
from typing import List, Tuple
from dataclasses import dataclass
import tiktoken
from rag_pipeline.processing.ai_client import deepseek_chat
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.storage.storage import StorageManager

logger = setup_logger()

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
        model: str = "deepseek",
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

    def extract_from_window(self, window_text: str, thinker_name: str, window_num: int, total_windows: int) -> List[str]:
        # System prompt: lock the model into extractor mode
        system_prompt = (
            "You are a deterministic policy/document extractor. Your only job is to emit verbatim institutional policy and process content as atomic concepts, suitable for retrieval-augmented chat. You must not explain, reason, or comment. You must not add headings, paraphrases, or summaries."
        )

        # User prompt: define what counts as a “concept” and the ONLY allowed output format
        user_prompt = f"""
        Extract institutional policy concepts from the text below.  
        Each extract must be output in the following format only:  

        EXTRACT: <verbatim concept>  

        Rules for what counts as a concept:  
        - Protocol requirements (e.g. “protocols must be complete to be assigned to a Panel”)  
        - Timeframes and deadlines (e.g. “Regular review takes 4–6 weeks”)  
        - Procedures or steps (e.g. “protocol submission is done online using the eProtocol system”)  
        - Conditions and exceptions (e.g. “under no circumstances may research begin until approval letter is received”)  
        - Oversight or responsibility assignments (e.g. “student investigators must list an academic sponsor”)  
        - Compliance or registration obligations (e.g. “all clinical trials must register at ClinicalTrials.gov”)  

        Exclusions:  
        - Navigation, headings, menus, contact details, generic boilerplate.  
        - Lists of offices or org charts unless they contain requirements.  
        - Any commentary, reasoning, or explanation from you.  

        If a concept spans multiple lines, merge them into a single verbatim extract.  
        Output **only EXTRACT lines**. Do not output anything else.  

        --- BEGIN TEXT ---
        {window_text}
        --- END TEXT ---

        Output format:
        EXTRACT: Under no circumstances may research begin until the Protocol Director has received a Notice of Certification or an IRB Approval Letter.
        EXTRACT: All Regular protocols must be presented, discussed and voted on at a convened meeting of the IRB.
        EXTRACT: Federal regulations allow for some protocols involving minimal risk to be reviewed by a single IRB member.
        EXTRACT: Student investigators must list an academic sponsor on the protocol.
        EXTRACT: All clinical trials meeting HHS regulations and NIH policy must register at ClinicalTrials.gov.
        """

        try:
            print(f"   Processing window {window_num}/{total_windows}...")
            content = deepseek_chat(
                user_prompt,
                model=self.model,
                temperature=0.2,    # be crisp
                max_tokens=800,     # chat model limit-friendly
                system_prompt=system_prompt,
            )
            extracts = self._parse_extracts(content)
            print(f"      → Extracted {len(extracts)} thought structures")
            self.stats.input_tokens += len(user_prompt)
            self.stats.output_tokens += len(content)
            return extracts
        except Exception as e:
            print(f"   ⚠️ Error processing window {window_num}: {e}")
            return []

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

    def save_to_jsonl(self, extracts: List[str], output_file: str):
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            for extract in extracts:
                f.write(json.dumps({"text": extract}, ensure_ascii=False) + '\n')
        logger.info(f"Saved JSONL locally: {output_file}")

        # optional GCS mirror
        if os.getenv("STORAGE_MODE", "local").lower() == "gcs":
            try:
                storage = StorageManager("gcs")
                storage.save_file(output_file, open(output_file).read())
                logger.info(f"[GCS PUSH] {output_file} → GCS")
            except Exception as e:
                logger.error(f"GCS mirror failed for {output_file}: {e}")

    def process_file(self, input_file: str, output_file: str, thinker_name: str) -> int:
        self.stats.start_time = time.time()

        print(f"🚀 Sliding Window Processing")
        print(f"📁 Input: {input_file}")
        print(f"🎯 Output: {output_file}")
        print(f"🧠 Thinker: {thinker_name}")
        print(f"🤖 Model: {self.model}")
        print("=" * 60)

        with open(input_file, 'r', encoding='utf-8') as f:
            text = f.read()

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

        print(f"\n💾 Saving to {output_file}...")
        self.save_to_jsonl(unique_extracts, output_file)

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
        print(f"📁 Output file: {output_file}")
        print(f"🎯 Ready for training pipeline!")

        return len(unique_extracts)


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
    parser.add_argument("--model", default="deepseek", help="AI model to use")   # <- changed default
    parser.add_argument("--window-size", type=int, default=25000, help="Window size in tokens")
    parser.add_argument("--overlap", type=int, default=8000, help="Overlap size in tokens")

    args = parser.parse_args()

    sliding_parser = SlidingWindowParser(
        model=args.model,
        window_size=args.window_size,
        overlap=args.overlap,
    )

    try:
        count = sliding_parser.process_file(args.input_file, args.output_file, args.thinker)
        print(f"\n🎉 Success! Generated {count} extracts for your pipeline.")
        return 0
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return 1

if __name__ == "__main__":
    exit(main())

