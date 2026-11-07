# Leverage Signal Sniper

Kaldıraç, arbitraj, haber ve tartışan yapay zeka masasını tek yerde toplayan kripto botu.

**Şu an sadece sahte parayla çalışır. Kodda gerçek emir gönderen hiçbir parça yoktur.**

## Neler yapar

| Parça | Ne yapar |
|---|---|
| Kural kasaları | 2 stratejiyi Binance (CEX) ve Hyperliquid'de (DEX) ayrı ayrı sahte parayla işler |
| Masa kasaları | Ajanların kararını yine CEX ve DEX'te ayrı ayrı işler |
| Haber | CoinDesk, Cointelegraph, Decrypt ve The Block'tan başlık toplar, önemli olanı işaretler |
| Masa | 3 analist, boğa ve ayı tartışması, risk sorumlusu, baş trader |
| Arbitraj | Vadeli CEX-CEX, vadeli CEX-DEX, spot CEX-CEX, spot CEX-DEX, DEX-DEX ölçer |
| Telegram | Durum, pozisyon, tartışma, haber, arbitraj, gider ve test raporları |

Her kasa 100$ ile başlar. Kasalar birbirinden ayrıdır, böylece karşılaştırılabilir.

## Hesaplanan giderler

- Giriş ve çıkış komisyonu (borsanın temel oranı)
- Gerçek tahtadan hesaplanan kayma
- Gerçek fonlama oranı
- Tasfiye (izole teminat)
- Transfer ve ağ ücretleri (tahmini, raporda öyle yazar)
- Yapay zeka API ücreti (her çağrıda ölçülür, masanın sonucundan düşülür)

Doluş kuralları kötümserdir. Mum hem stopa hem hedefe değerse stop sayılır. Limit hedef,
fiyat içinden geçmeden dolmaz. Stop, tahtadan ve stoptan kötü olanından dolar.

## Risk sınırları

Sadece `tradebot/settings.py` belirler. Hiçbir ajan bunları değiştiremez.

| Kural | Değer |
|---|---|
| İşlem başına risk (masraf dahil) | 1,50$ |
| En yüksek kaldıraç | 5x, izole |
| Açık pozisyon | En çok 3, aynı yönde en çok 2 |
| Günlük zarar durdurma | 5$ |
| Kasa durdurma | 25$ zarar |

## Masa nasıl korunuyor

- Ajanlar sadece yön, güven ve yarım veya tam boy seçer. Parite, büyüklük, stop, hedef ve
  kaldıraç kodda belirlenir.
- Haber başlıkları güvenilmeyen metindir. Temizlenir ve ajanlara "talimat değil, iddia" diye verilir.
- Bir haber tek başına işlem açtıramaz. Teknik veya akış analisti aynı yönde olmalıdır.
- Hata, red, eksik yanıt, bütçe aşımı: hepsinin sonucu "işlem yok".
- Tartışma bittikten sonra fiyat çok oynadıysa işlem açılmaz.

## Geçmiş test sonuçları (7 Kasım 2026)

2 yıl, 10 parite, tüm masraflar dahil. Parametre ilk %60'ta seçildi, son %40 bir kez ölçüldü.
R, işlem başına riske edilen paradır.

| Strateji | İşlem başı | İşlem | Rastgeleyi geçme |
|---|---|---|---|
| 4 saatlik trend kırılımı | +0,03R | 99 | %67 |
| Aşırı fonlamaya karşı | +0,12R | 31 | %89 |
| 1 saatlik trend kırılımı | -0,07R | 277 | %48 |
| Aşırı alım-satımdan dönüş | -0,21R | 204 | %2 |
| Eski 15 dakikalık sinyal (180 gün) | -0,12R | 359 | rastgeleden iyi değil |

**Hiçbiri rastgeleden anlamlı şekilde iyi değil.** Bot ilk ikisini kanıtlandığı için değil,
en az kötü olduğu için izliyor. Yapay zeka masası geçmişte test edilemedi.

Tekrar çalıştırmak için:

```
python -m tradebot.research --days 730
python -m tradebot.backtest --days 180
```

## Kurulum

```
pip install -r requirements.txt
python main.py
```

`.env` dosyası:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ANTHROPIC_API_KEY=...       # Yoksa masa kapalı çalışır, kalanı devam eder
DESK_MODEL=claude-opus-5-5  # claude-sonnet-5-5 veya claude-haiku-5-5 de olur
DESK_DAILY_BUDGET_USD=1.0
```

Testler:

```
pip install pytest pytest-asyncio
python -m pytest
```

## Telegram komutları

`/durum` `/pozisyon` `/islemler` `/masa` `/haber` `/arbitraj` `/maliyet` `/test` `/kanit` `/sifirla`

Bot, sohbete sabitlenmiş bir yedek dosyası tutar. Sunucu yeniden kurulsa da geçmiş kaybolmaz.
Bu dosyayı silmeyin.
