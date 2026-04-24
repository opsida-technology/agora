# Running Agora

Pratik komutlar: başlat, durdur, log'la, troubleshoot.

## Başlatma

```bash
cd ~/development/agora_protocol

# Dashboard (default port 8420)
python3 -m agora.web

# Farklı port
python3 -m agora.web --port 8321

# Auto-start a debate
python3 -m agora.web configs/lmstudio_dual.yaml

# Arka planda (nohup)
nohup python3 -m agora.web --port 8420 > /tmp/agora.log 2>&1 &
```

Tarayıcıda aç: **http://localhost:8420/**

## Durdurma

```bash
# Tüm Agora sunucularını öldür
pkill -f "agora.web"

# Belirli port
lsof -ti :8420 | xargs kill -9
```

## Durum kontrolü

```bash
# Çalışan process'ler
pgrep -af "agora.web"

# Dinleyen port
lsof -nP -iTCP:8420 -sTCP:LISTEN

# API health
curl -s http://localhost:8420/api/status | python3 -m json.tool
```

## Log izleme

```bash
# Foreground başlattıysan: zaten terminalde
# Arka plandaysa:
tail -f /tmp/agora.log
```

## LM Studio ile

1. LM Studio → modelleri **Load** et
2. **Developer** → **Start Server** (default: `localhost:1234`)
3. `configs/lmstudio_dual.yaml` config'ini kullan (2 modelli örnek)
4. Endpoint kontrolü:
   ```bash
   curl http://localhost:1234/v1/models
   ```

## Ollama ile

```bash
ollama serve
ollama pull qwen3.5:9b
ollama pull gemma4:e4b
# configs/ollama_local.yaml kullan
```

## Testler

```bash
python3 -m pytest tests/ -v
```

## Sık karşılaşılan sorunlar

**Port already in use**
```bash
lsof -ti :8420 | xargs kill -9
```

**LM Studio 404 on /v1/chat/completions**
- Server başlatılmış mı? Developer sekmesinden "Start Server"
- Model load edildi mi? UI'da yeşil tik olmalı

**CLI backend timeout**
- `claude` / `gemini` / `codex` komutları PATH'te mi? `which claude`
- `timeout:` değerini YAML'de artır (default 120s)

**Debate başlamıyor**
- `/api/status` ne diyor? `curl http://localhost:8420/api/status`
- Config'i doğrula: `python3 -c "import yaml; print(yaml.safe_load(open('configs/X.yaml')))"`
- Logs'ta health check hatası var mı?

## Remote (Mac Mini üzerinden)

Tailscale IP: `100.90.207.106`

```bash
# SSH
ssh ozgun@100.90.207.106

# Local'den başlat
ssh ozgun@100.90.207.106 "cd ~/agora_protocol && nohup python3 -m agora.web > /tmp/agora.log 2>&1 &"

# Tarayıcıdan
open http://100.90.207.106:8420/

# Proje güncelleme (local → mini)
rsync -avz --exclude '.git' --exclude 'debates/' --exclude '__pycache__' \
  ~/development/agora_protocol/ ozgun@100.90.207.106:~/agora_protocol/
```
