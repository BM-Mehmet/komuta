# Komuta

Komuta, Linux masaüstü için klavye odaklı bir başlatıcı ve pano geçmişi uygulamasıdır. Metin ve görselleri yerel SQLite veritabanında tutar; dosya kopyalamalarında dosyaları çoğaltmak yerine yollarını kaydeder. Kayıtları kategorilere taşımaya, sabitlemeye ve aramaya izin verir. Kurulu uygulamalar ile Masaüstü, Belgeler, İndirilenler ve görsel/video klasörlerindeki dosyalar da aranabilir.

## Kurulum

Python 3.10+ ve PyQt6 gerekir. Arch Linux üzerinde:

```bash
sudo pacman -S python-pyqt6
./install.sh
```

`install.sh`, kullanıcı uygulama menüsüne gizli bir kısayol ve oturum başlangıç girdisi ekler; KDE Plasma'da Alt+Boşluk kısayolunu ayarlar. Kısayol başka bir eylemde kullanılıyorsa Plasma'nın Kısayollar ayarlarından Komuta'ya ata. KDE kısayolu, açık uygulamaya D-Bus isteği gönderir; her tuş basışında yeni bir Komuta/Python süreci oluşturmaz. Başka masaüstlerinde uygulamayı `./launch.sh` ile başlat. Kaynak klasörünü taşımak kurulu kısayolları bozar; taşıdıktan sonra kurulum betiğini yeniden çalıştır.

## Kullanım

- `Alt+Boşluk`: arama penceresini aç/kapat (KDE Plasma kurulumundan sonra).
- Arama kutusu: pano kayıtlarında, uygulamalarda ve taranan klasörlerde arama yap.
- Enter: pano kaydını kopyala; uygulamayı başlat; dosyayı varsayılan uygulamayla aç.
- Bağlantılar ve görseller otomatik kategorilenir. Pano kaydına sağ tık: kategoriyi değiştir, sabitle, kopyala veya sil.
- Sol menüdeki `+`: yeni pano kategorisi ekle.
- Escape: pencereyi gizle. Tepsi simgesinden yeniden açabilir veya uygulamadan çıkabilirsin.
- `./launch.sh` zaten açıkken tekrar çalışırsa yeni örnek başlatmaz; var olan pencereyi öne getirir.

Pano geçmişi `~/.local/state/komuta/clipboard.db` altında tutulur. Metinler ve dosya yolları 200 KB, tek dosya listesi 100 öğe, görseller öğe başına 8 MB ve toplam 100 MB ile sınırlıdır; sınırı aşan yeni içerik kaydedilmez. Dosyaların kendisi kopyalanmaz, yalnızca yolları saklanır; dosya taşınır veya silinirse geçmişteki kopyası da kullanılamaz. Görseller listede küçük önizlemeyle gösterilir. En yeni 200 normal kayıt ve en fazla 50 sabitlenmiş kayıt tutulur. Uygulama pano metin ve görsellerini diske yazar; gizli bilgi içeren kopyalamalara dikkat et. Geçmişi arayüzden temizleyebilirsin.

## Geliştirme

```bash
python3 app.py
```

Uygulama ve dosya listeleri açılışta arka planda taranır. Pano araması anlık, yerel ve ağ gerektirmez.
