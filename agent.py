import sys
import subprocess
from pathlib import Path
import shutil
import os
import requests
import re

AGENT_DIR = Path("H:/AI/Agents/SWE/.llm-orc").resolve()

from agents_md import load_rules, rules_for   # new import at top

def get_project_context(target_file_path, role="coder"):
    text, src = load_rules(target_file_path, Path(__file__).parent)
    if src:
        print(f"📄 Found project rules: {src}")
    return rules_for(text, role)

def get_routing_decision(user_prompt):
    print("🧠 Asking Conductor (Qwen 3B) for routing decision...")
    url = "http://localhost:1234/v1/chat/completions"
    payload = {
        "model": "qwen2.5-3b-instruct",
        "messages": [
            {"role": "system", "content": "Output EXACTLY one word: 'CODE' if the request involves programming, HTML structure, CSS, or bug fixes. Output 'COPY' if it involves marketing text, SEO, or prose. Do not explain."},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 5
    }
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()['choices'][0]['message']['content'].strip().upper()
        return "code-workflow" if "CODE" in result else "copy-workflow"
    except Exception as e:
        print(f"⚠️ Conductor failed ({e}). Defaulting to copy-workflow.")
        return "copy-workflow"

def main():
    if len(sys.argv) < 3:
        print("Usage: python H:\\AI\\Agents\\SWE\\agent.py <file_path> \"<instruction>\"")
        sys.exit(1)

    target_file = Path(sys.argv[1]).resolve()
    user_prompt = sys.argv[2]

    if not target_file.exists():
        print(f"❌ Error: File {target_file} not found.")
        sys.exit(1)

    print(f"📂 Target: {target_file}")
    print(f"🎯 Goal: {user_prompt}")

    ensemble_name = get_routing_decision(user_prompt)
    print(f"🚦 Routing to: {ensemble_name}")

    file_content = target_file.read_text(encoding="utf-8")
    project_context = get_project_context(target_file)

    full_prompt = f"""
    USER INSTRUCTION: {user_prompt}
    
    {project_context}
    
    TARGET FILE ({target_file.name}):
    {file_content}
    """

    print(f"⏳ Orchestrating agents using {ensemble_name} (this may take a minute)...")

    venv_orc = Path("H:/AI/Agents/SWE/.venv/Scripts/llm-orc.exe")
    orchestra_exe = str(venv_orc) if venv_orc.exists() else shutil.which("llm-orc")

        # REMOVED --input to avoid Windows 32k command-line length limit
    cmd = [orchestra_exe, "invoke", ensemble_name, "--config-dir", str(AGENT_DIR)]

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        temp_out = "H:\\AI\\Agents\\SWE\\temp_out.txt"
        
        # Use Popen to stream output AND send the prompt via stdin
        with open(temp_out, "w", encoding="utf-8") as f:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,   # <--- Added this
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                encoding="utf-8"
            )
            
            # Write the massive prompt to stdin and close it to signal EOF
            process.stdin.write(full_prompt)
            process.stdin.close()
            
            # Read output line by line
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                
                f.write(line)
                
                clean_line = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', line)
                clean_line = clean_line.replace('\r', '').strip()
                
                if clean_line and not all(c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" for c in clean_line):
                    if "│" in clean_line or "──" in clean_line or "Results" in clean_line:
                         print(f"  {clean_line}")
                    elif clean_line.strip() == "==================================================":
                         print("\n" + "="*50)
                    else:
                         print(clean_line)

            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)
            
        raw_output = Path(temp_out).read_text(encoding="utf-8")
        
        # 1. Strip ANSI escape codes (color codes and spinners)
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_output = ansi_escape.sub('', raw_output)

        # 2. Extract only the text from the Results section
        if "📊 Performance Summary" in clean_output:
            results_section = clean_output.split("📊 Performance Summary")[0]
        else:
            results_section = clean_output

        # 3. Split by the agent completion markers (e.g., ✓ prose_critic)
        agent_blocks = re.split(r'✓ [a-zA-Z_]+ \([^)]+\)', results_section)
        final_text = agent_blocks[-1].strip()

        # 4. Remove trailing and leading separators
        final_text = re.sub(r'^-+$', '', final_text, flags=re.MULTILINE).strip()

                # 5. Extract content inside markdown code blocks if they exist
        code_block_match = re.search(r'```(?:html|css|javascript|python|json|text)?\n(.*?)```', final_text, re.DOTALL)
        if code_block_match:
            final_text = code_block_match.group(1).strip()
        else:
            final_text = final_text.strip('`').strip()

        # 🛡️ SAFETY CHECK 1: Length check
        if len(final_text) < 100 and len(file_content) > 500:
            print("\n❌ SAFETY ABORT: The model output was suspiciously short. Refusing to overwrite the file.")
            sys.exit(1)

        # 🛡️ SAFETY CHECK 2: HTML Structural Integrity
        if target_file.suffix == '.html':
            # Strip the APPROVED comment for the check
            check_text = final_text.replace('<!-- APPROVED -->', '').strip()
            if not check_text.endswith('</html>'):
                print("\n❌ SAFETY ABORT: The model output is truncated. Missing closing </html> tag.")
                print(f"The file ended with: ...{final_text[-100:]}")
                sys.exit(1)

        # Strip the APPROVED comment for the final output (optional, but cleaner)
        final_text = final_text.replace('<!-- APPROVED -->', '').strip()

        # Save backup and write
        backup_file = target_file.with_suffix(target_file.suffix + ".bak")
        if backup_file.exists():
            backup_file.unlink()
        target_file.rename(backup_file)
        
        target_file.write_text(final_text, encoding="utf-8")
        print(f"\n✅ Success! Updated {target_file.name} (Backup saved as {backup_file.name})")
        
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Orchestrator failed. Check {temp_out} for details.")

if __name__ == "__main__":
    main()