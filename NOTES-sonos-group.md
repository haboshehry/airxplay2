# حالت پایدار و نهایی — گروه AirPlay 2 با Sonos

تاریخ: 2026-11-09 · ایمیج: `zima-airplay2:good-2026-11-09`

## تنظیمات تأییدشده (در `/data/config.json` ذخیره شده)
| کلید | مقدار | چرا |
|---|---|---|
| `latency_offset_ms` | `-50` | جبران تأخیر مسیر خروجی (HDMI/PCM). صفر کردنش باعث می‌شود فریم‌ها «دیررسیده» حساب شوند و resync بی‌پایان بدهد. |
| `stable_44k` | `true` | خروجی روی 48k قفل می‌شود تا با ورودی 44.1k گروه، HDMI دوباره مذاکره نکند. |
| `device` | `plughw:CARD=PCH,DEV=3` | خروجی صدا. |
| `verbose` | `true` | لاگ `-vv` برای عیب‌یابی (نام متدهای RTSP). |

در فایل shairport:
- `audio_backend_buffer_desired_length_in_seconds = 1.0`
- `audio_decoded_buffer_desired_length_in_seconds = 2.0`
- `audio_backend_latency_offset_in_seconds = -0.050`

## ترتیب درست انتخاب (مهم)
**اول `ZimaOS AirPlay`، بعد Sonos.** حالت برعکس (Sonos-first) با این استک کار نمی‌کند: آیفون ۷۵ میلی‌ثانیه بعد از `player_play` جلسه را می‌بندد.

## چه چیزی در NQPTP پچ شده
فایل `nqptp` (نسخه 1.2.9) داخل ایمیج، در `/usr/local/bin/nqptp`؛ نسخه اصلی در `nqptp.orig`.
1. master clock = همان کلاکی که ردیابی می‌شود (نه هویت grandmaster اعلام‌شده) تا شناسه وسط جلسه نپرد.
2. `clock_change_offset` همیشه صفر → `anchor_time` با اختلاف مبنای زمانی (روز) جابه‌جا نمی‌شود.
3. کلاکی که مبنای زمانی‌اش بیش از ۶۰ ثانیه با master فعلی فرق دارد نادیده گرفته می‌شود.

پنل هم فهرست peer را دقیقاً مطابق کلاکی که آیفون به‌عنوان anchor اعلام می‌کند تنظیم می‌کند (`sender_ip()` یا IPهای Sonos).

## چه چیزی امتحان شد و رد شد (تکرار نکنید)
- **فهرست peer به‌صورت union (آیفون + همه Sonosها):** کشنده است. offset از کلاکی می‌آید که مبنایش چند روز فرق دارد و هر فریم lead time ≈ ۳۵۳٬۵۳۹ ثانیه می‌گیرد → جلسه «در حال پخش» می‌ماند ولی هیچ صدایی تولید نمی‌شود.
- **`latency_offset = 0`:** پخش را خراب می‌کند (همان resync بی‌پایان).
- **ری‌استارت خودکار بعد از هر جلسه پرتلاطم:** کاربر آن را «ریست شدن Zima» می‌بیند؛ حالا فقط بعد از ۶۰ ثانیه سکوت، حداکثر هر ۱۰ دقیقه.

## بازگشت به نسخه اصلی NQPTP
```bash
docker cp airplay2:/usr/local/bin/nqptp.orig airplay2:/usr/local/bin/nqptp
docker restart airplay2
```

## بازسازی کامل ایمیج
```bash
cd /DATA/AppData/airplay2 && sh build_image.sh
sudo docker compose -f /var/lib/casaos/apps/custom__f9exlpd3/docker-compose.yml up -d --force-recreate
```
