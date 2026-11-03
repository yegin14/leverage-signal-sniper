# ⚡ Leverage Signal Sniper (Sub-Second Engine)

Bu proje, kripto para vadeli işlem (Futures) piyasalarını **saniyenin altında (<500 ms)** asenkron olarak tarayan, çok faktörlü matematiksel filtrelerden geçirdikten sonra **%80+ güven skoruna** sahip sinyalleri Telegram'a ileten yüksek hızlı bir sinyal motorudur.

---

## 🎯 Projenin Temel İlkeleri

1. **Rastgele Değil, Matematiksel Olarak Hesaplanmış:**
   - **Makro Trend:** 15 dakikalık grafikte `EMA 20` ve `EMA 50` yönü.
   - **Hacim Patlaması (RVOL):** Son mum hacmi, son 20 mum ortalamasının en az `1.8x - 2.5x` katı olmalı (Akıllı para girişi).
   - **Kırılım ve Volatilite:** Bollinger Üst/Alt bant kırılımı veya 20 periyotluk Donchian kanal patlaması.
   - **RSI İvmesi:** Yükselişte 50-75 arası, düşüşte 25-50 arası ivme kanalı.
   - **Fonlama Eğilimi (Sentiment):** Zıt yöndeki sıkışmaları (Short Squeeze vb.) tespit eder.

2. **Dinamik Kasa Yönetimi ($100 Bütçe İçin Tasarlandı):**
   - **Sabit Risk:** İşlem stop olursa kasadan **tam olarak $2.00 (%2.0)** eksilir.
   - **Dinamik Stop:** ATR (Ortalama Gerçek Aralık) çarpanı ile belirlenir.
   - **Kaldıraç:** Stop mesafesine göre dinamik hesaplanır (Örn: Stop %1.5 ise ~13x kaldıraç ve ~$10 margin).
   - **Kademeli Kâr:** TP1 (1.5R = +$3), TP2 (2.5R = +$5), TP3 (4.0R = +$8).

3. **Sub-Second Hız:**
   - NumPy tabanlı saf vektörize matematik.
   - Analiz süresi: **~1.5 milisaniye**!
   - Toplam ağ döngüsü: **~600 milisaniye** (Saniyenin altında).

---

## 🚀 Nasıl Çalıştırılır?

### Yöntem 1: Çift Tıklayarak
Masaüstündeki `LeverageSignalSniper` klasörüne girip **`BAŞLAT.bat`** dosyasına çift tıklayın.

### Yöntem 2: Terminalden
```powershell
cd C:\Users\Halil\Desktop\LeverageSignalSniper
python main.py
```

### Hız Testi (Benchmark):
```powershell
python test_signal.py
```

---

## 📱 Telegram Bildirim Ayarı

1. Bot token varsayılan olarak tanımlıdır.
2. Kendi Telegram ID'nizi öğrenmek için Telegram'da `@userinfobot` botuna `/start` yazıp ID'nizi alın.
3. `C:\Users\Halil\Desktop\LeverageSignalSniper\.env` dosyasını açıp `TELEGRAM_CHAT_ID=` karşısına ID'nizi yazıp kaydedin.
