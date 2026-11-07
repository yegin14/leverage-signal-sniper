# Kaldıraç ve Arbitraj Botu

Kripto için kaldıraç, arbitraj ve haberle pozisyon alan bot. Her şey web panelinde görünür.

**Şu an sadece sahte parayla çalışır. Kodda gerçek emir gönderen hiçbir parça yoktur.**

## Neler yapar

| Parça | Ne yapar |
|---|---|
| Kaldıraç | 4 stratejiyi Binance (CEX) ve Hyperliquid'de (DEX) ayrı ayrı işler |
| Haber | 4 kaynaktan başlık toplar. Olumlu haberde long, olumsuz haberde short açar |
| Arbitraj | Vadeli CEX-CEX, vadeli CEX-DEX, spot CEX-CEX, spot CEX-DEX, DEX-DEX ölçer. Masraf sonrası kârlıysa işlem yapar |
| Yapay zeka masası | API anahtarı girilince çalışır: analistler, boğa-ayı tartışması, risk sorumlusu, baş trader |
| Web paneli | Pozisyon, kâr/zarar, bakiye, işlemler, haberler, tartışmalar, giderler, olay günlüğü |
| Telegram | Kısa durum, panel linki, 6 saatte bir özet ve önemli uyarılar |

Her strateji her piyasada ayrı 100$ kasa ile başlar, sonuçlar birbirine karışmaz.

## Deneme süreci kuralları

İşlem sayısı sınırı yoktur. Açık pozisyon, aynı yön, günlük zarar, toplam zarar ve bekleme
sınırları kapalıdır (`tradebot/settings.py`, 0 kapalı demektir). **Gerçek paradan önce açılmalıdır.**

Kalan kurallar:

- İşlem başına en çok 1,50$ risk, masraf dahil
- En çok 10x, izole teminat. Tasfiye fiyatı stoptan en az iki kat uzakta tutulur
- Aynı kasada bir paritede tek pozisyon
- Teminat bitince yeni işlem açılmaz
- Yapay zeka harcaması günlük 1$ ile sınırlı, çünkü o gerçek paradır

## Hesaplanan giderler

- Giriş ve çıkış komisyonu (borsanın temel oranı)
- Gerçek tahtadan hesaplanan kayma
- Gerçek fonlama oranı
- Tasfiye
- Transfer ve ağ ücretleri (tahmini, panelde öyle yazar)
- Yapay zeka API ücreti (her çağrıda ölçülür)

Doluş kuralları kötümserdir. Mum hem stopa hem hedefe değerse stop sayılır. Limit hedef,
fiyat içinden geçmeden dolmaz. Stop, tahtadan ve stoptan kötü olanından dolar.

## Haberle pozisyon

Yönü anahtar kelimeler belirler. Yöntem kabadır ve bağlamı kaçırabilir. Geçmiş testi yoktur, çünkü
RSS kaynakları eski haberi tutmaz. Panel her başlığı açtığı pozisyonla yan yana gösterir.

Fiyat hareketini anlatan kelimeler ("surges", "plunges") yön sayılmaz, çünkü hareket zaten olmuştur.
İki yöne de işaret eden başlıkta ve 30 dakikadan eski haberde işlem açılmaz.

## Geçmiş test sonuçları (7 Kasım 2026)

Tüm masraflar dahil, ilk 10 parite. Parametre verinin ilk %60'ında seçildi, sonuç son %40'ta bir
kez ölçüldü.

| Strateji | Net | İşlem | Dönem |
|---|---|---|---|
| Aşırı fonlama | +5,7$ | 31 | Son 9,5 ay |
| 4 saatlik trend | +3,9$ | 99 | Son 9,5 ay |
| 1 saatlik trend | -27,3$ | 277 | Son 9,5 ay |
| Aşırı alım-satımdan dönüş (botta yok) | -57,5$ | 204 | Son 9,5 ay |
| 15 dakikalık sinyal | -59,4$ | 359 | Son 72 gün |

**Hiçbiri rastgele işlemden anlamlı şekilde iyi değil.** Zarar edenler de deneme sürecinde çok
işlem üretmek için çalışıyor. Yapay zeka masası ve haber geçmişte test edilemedi.

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

Panel `http://localhost:8080` adresinde açılır. `.env` dosyası:

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

`/durum` `/panel` `/sifirla`

Bot, sohbete sabitlenmiş bir yedek dosyası tutar. Sunucu yeniden kurulsa da geçmiş kaybolmaz.
Bu dosyayı silmeyin.

## Panel erişimi

Panel adresi bilen herkes tarafından görülebilir ve sadece okunur. Sahte para verisi dışında bir
şey göstermez. Gerçek paraya geçmeden önce şifre eklenmelidir.
