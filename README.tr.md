# mcp-output-guard

**MCP araç çıktısı ajanın bağlam penceresini doldurduğunda devreye girer: sonucu kısaltır, tam
içeriği yerel bir dosyaya yazar ve modele neyin kesildiğini, kalanın nerede olduğunu açıkça
söyler.**

Herhangi bir [Model Context Protocol](https://modelcontextprotocol.io) sunucusu için stdio ara
katman. İstemciyi sunucuya değil, bu proxy'ye bağlarsınız. Sunucuda değişiklik yok, SDK yok,
çalışma zamanı bağımlılığı yok — sadece Python standart kütüphanesi.

```
MCP istemcisi  ──stdio──▶  mcp-output-guard  ──stdio──▶  herhangi bir MCP sunucusu
                                 │
                                 └──▶  ~/.mcp-guard/spills/<sha256>.txt   (tam çıktı)
```

## Neden

Ajanları bitiren şey genelde istem (prompt) değil, araç çıktısıdır. Bir veritabanı sorgusu
40 000 satır, bir tarayıcı anlık görüntüsü 3 MB DOM, bir log okuyucu 50 MB döndürür; oturum ya
bağlam penceresini aşar ya da sessizce kırpılır:

- Claude Code araç sonuçlarını ~25K token ile sınırlıyor, fazlasını dosyaya yazıyor
  ([anthropics/claude-code#45770](https://github.com/anthropics/claude-code/issues/45770)).
- Storybook MCP büyük depolarda çıktı sınırını aşıyor
  ([storybookjs/mcp#311](https://github.com/storybookjs/mcp/issues/311)).
- LM Studio 50K karakter üstünü uyarısız `(truncated)` diye kesiyor
  ([lmstudio-ai/lmstudio-bug-tracker#2475](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/2475)).
- Google AI Edge Gallery sınırsız sonuçta sohbeti çökertiyor
  ([google-ai-edge/gallery#889](https://github.com/google-ai-edge/gallery/issues/889)).

Bu sorunların hepsi sunucu tarafında, sunucuyu yazan kişi tarafından çözülüyor — ya da hiç
çözülmüyor. `mcp-output-guard` işi istemci tarafına taşır: elinizde olmayan sunucular dahil her
sunucu için çalışır ve kesmeyi gizlemez. Model `truncated: true` bayrağını, toplam boyutu ve
dosya yolunu görür.

Mevcut seçenekler daha dar: `mcp-cache` önbellekler, `mcp-rlm-proxy` yapılandırılmış kesme
bilgisi vermez, FastMCP'nin `ResponseLimitingMiddleware`'i ise yalnızca FastMCP ile yazılmış
sunucularda çalışır — satın aldığınız/indirdiğiniz bir sunucuyu koruyamaz.

## Kurulum

```bash
pipx install mcp-output-guard      # veya: pip install mcp-output-guard
```

Python 3.10+ gerekir. Çalışma zamanı bağımlılığı yoktur.

## 30 saniyelik örnek

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_logs"}}' \
  | mcp-output-guard run --max-chars 400 -- python examples/demo_server.py
```

Gerçek çıktı (okunabilirlik için biçimlendirildi; araç 37 149 karakter döndürdü):

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {"type": "text", "text": "2026-01-01T00:00:00Z INFO worker-0 job=fake-job-000 ... (400 karakter)"},
      {"type": "text", "text": "\n\n[TRUNCATED: 37149 chars total, 400 shown, full output: /home/you/.mcp-guard/spills/85650bc8....txt]"}
    ],
    "_meta": {
      "truncated": true,
      "output_guard": {
        "truncated": true,
        "tool": "read_logs",
        "max_chars": 400,
        "total_chars": 37149,
        "shown_chars": 400,
        "spill": "/home/you/.mcp-guard/spills/85650bc8....txt"
      }
    }
  }
}
```

Dosyada 37 149 karakterin tamamı var. Model sohbetten çıkmadan dosyada gezinebilir, çünkü proxy
`tools/list` yanıtına dört araç ekliyor:

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"output_guard_read",
  "arguments":{"spill":"85650bc8....txt","offset":0,"limit":200}}}
```

Yanıt:

```
/home/you/.mcp-guard/spills/85650bc8....txt
chars 0..200 of 37149 (eof=False)
---
2026-01-01T00:00:00Z INFO worker-0 job=fake-job-000 host=fake-host-0 status=ok latency_ms=0
...
```

## İstemcinize bağlama

Sunucu komutunu proxy ile sarmalayın, başka bir şey değişmiyor.

`claude_desktop_config.json` / `.mcp.json` / Cursor / Claude Code:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "mcp-output-guard",
      "args": ["run", "--max-chars", "20000", "--", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/work"]
    }
  }
}
```

Windows'ta `npx.cmd` gibi sarmalayıcılar otomatik çözülür; aynı satır orada da çalışır.

## Bir sonuca ne yapıyor

| Durum | Davranış |
| --- | --- |
| Sonuç sınırın altında | **Bayt bayt** aynen geçer, yeniden serileştirilmez |
| Metin sınırın üstünde | Baştan N karakter + `[TRUNCATED: …]` notu, tam metin dosyada |
| `isError: true` | Hiç dokunulmaz — bozulmuş bir traceback uzun olanından kötüdür |
| JSON-RPC hata yanıtı | Hiç dokunulmaz |
| Birden çok `content` bloğu | Bütçe sırayla paylaşılır, artan bloklar notta sayılır |
| Çok baytlı metin | Karakter sınırında kesilir; kod noktası veya surrogate çifti bölünmez |
| `structuredContent` sınırın üstünde | İlk K kayıt kalır, şema bozulmaz, sayılar `_meta`'da |
| Base64 görsel/ses büyük | Çözülür, gerçek dosyaya yazılır, yerine yol + boyut konur |
| Bozuk base64 | Olduğu gibi bırakılır, `_meta.output_guard.binary` içinde işaretlenir |

Bayt bayt garantisi önemli: istemci çıktıyı özetliyor veya karşılaştırıyorsa, guard gerçekten
bir şey yapmadıkça hiçbir fark görmez.

## Sanal araçlar

Başarılı her `tools/list` yanıtına eklenir (`--no-virtual-tools` ile kapatılır):

| Araç | İşi |
| --- | --- |
| `output_guard_read(spill, offset, limit)` | Karakter penceresi; `total_chars` ve `eof` ile |
| `output_guard_grep(spill, pattern, ignore_case, max_matches)` | Satır numaralı regex arama |
| `output_guard_sql(spill, query, max_rows)` | JSON dizisi veya CSV üzerinde salt-okunur SQL |
| `output_guard_spills(limit)` | Son spill dosyalarını listeler |

`output_guard_sql` veriyi bellekte `data` adlı SQLite tablosuna yükler. Sütun tipleri çıkarılır,
böylece `WHERE id > 15` sayısal karşılaştırma olur; sorgudan önce bağlantı `query_only` moduna
alınır, yani spill dosyası asla değiştirilemez.

## Komut satırı

```
mcp-output-guard run --max-chars N [seçenekler] -- <sunucu komutu>

  --max-chars N            araç sonucu başına metin bütçesi (zorunlu)
  --spill-dir D            tam çıktıların yazılacağı yer (varsayılan ~/.mcp-guard/spills)
  --tool-limit NAME=N      araç bazlı sınır, tekrarlanabilir
  --image-max-chars N      bu karakterden büyük base64 yükler dosyaya alınır (varsayılan 1400000)
  --note-lang en|tr        kesme notunun dili (varsayılan en)
  --reserve-note           notu --max-chars'a eklemek yerine bütçenin içine say
  --no-virtual-tools       output_guard_* araçlarını ekleme
  --ttl-seconds N          `spills clean` için saklama süresi (varsayılan 604800)
  --stats                  çıkışta stderr'e kullanım özeti yaz

mcp-output-guard spills list|stats|clean [--json]
mcp-output-guard read <id|yol> [--offset N] [--limit N] [--grep REGEX] [--sql QUERY]
```

Spill klasörü `MCP_OUTPUT_GUARD_SPILL_DIR` ortam değişkeniyle de verilebilir.

## Kütüphane olarak

```python
from mcp_output_guard import SpillStore, limit_result

store = SpillStore("/tmp/spills")
result, info = limit_result(
    {"content": [{"type": "text", "text": "x" * 50_000}]},
    4_000,
    spill=store,
    tool="db.query",
)
assert info["truncated"] and info["total_chars"] == 50_000
```

Değişiklik gerekmediğinde `result` aynı nesne olarak, `info` ise `None` olarak döner; proxy
orijinal baytları bu sayede olduğu gibi iletebiliyor.

## Spill dosyaları hassas veri tutar

Araç çıktısı sık sık sır içerir: ortam değişkeni dökümleri, müşteri satırları, loglara düşmüş
token'lar. Guard şunları yapar:

- spill klasörünü `0700`, dosyaları `0600` izniyle oluşturur;
- dosyaları içeriğin SHA-256'sıyla adlandırır (aynı yük tekrar yazılmaz);
- spill klasörünün dışına çıkan yolları, sembolik bağlantı üzerinden bile, okumayı reddeder;
- `mcp-output-guard spills clean --ttl-seconds 3600` ile eski dosyaları siler.

Veriyi güvenli hale getirmez. **Spill klasörünü üretim verisi içeren bir log dosyası gibi
düşünün**; önemliyse `--spill-dir` ile şifreli veya geçici bir birim gösterin ve temizleyin. Bu
araç hiçbir şeyi ağa göndermez.

## Sınırlar (dürüstçe)

- Yalnızca **stdio** taşıması desteklenir. HTTP/SSE kapsam dışı.
- Kesme **karakter** ile ölçülür, token ile değil. Karakter bütçesi token'ın göstergesidir,
  ölçümü değil; CJK metin ve kod, düz yazıdan daha yoğundur.
- JSON-RPC **batch** isteklerinde sanal araçlar sunucuya iletilir, proxy yanıtlamaz.
- İç içe `structuredContent` en büyük listeden başlanarak budanır; keyfi şemalar için sorgu
  motoru değildir.
- Proxy tek süreç, tek bağlantı çalışır; çoklama yapmaz.

## Geliştirme

```bash
python -m pip install -e ".[dev]"
bash scripts/check.sh        # ruff + mypy + pytest, --cov-fail-under=85
```

Güncel durum: 171 test, %94 satır+dallanma kapsamı, Linux ve Windows CI.

## Lisans

MIT — [LICENSE](LICENSE).
