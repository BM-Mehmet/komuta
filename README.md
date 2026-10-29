# Komuta

Komuta, Linux masaüstü için terminal tarzı, klavye odaklı bir başlatıcı ve pano geçmişi uygulamasıdır. Metin ve görselleri yerel SQLite veritabanında tutar; dosya kopyalamalarında dosyaları çoğaltmak yerine yollarını kaydeder. Kayıtları kategorilere taşımaya, sabitlemeye ve aramaya izin verir. Kurulu uygulamalar ve dosya sistemi kökündeki (`/`) dosyalar aranabilir.

## Kurulum

Python 3.10+ ve PyQt6 gerekir. Arch Linux üzerinde:

```bash
sudo pacman -S python-pyqt6
./install.sh
```

`install.sh`, kullanıcı uygulama menüsüne gizli bir kısayol ve oturum başlangıç girdisi ekler; KDE Plasma'da Alt+Boşluk kısayolunu ayarlar. Kısayol başka bir eylemde kullanılıyorsa Plasma'nın Kısayollar ayarlarından Komuta'ya ata. KDE kısayolu, açık uygulamaya D-Bus isteği gönderir; her tuş basışında yeni bir Komuta/Python süreci oluşturmaz. Başka masaüstlerinde uygulamayı `./launch.sh` ile başlat. Kaynak klasörünü taşımak kurulu kısayolları bozar; taşıdıktan sonra kurulum betiğini yeniden çalıştır.

Kaldırmak için `./uninstall.sh` çalıştır. Bu, kısayol ve oturum kayıtlarını siler; pano geçmişini ve kaynak klasörünü korur. Pano geçmişini de silmek için `./uninstall.sh --purge-data` kullan. Uygulama açıksa kaldırmadan önce tepsi menüsünden çıkış yap; kaynak klasörünü tamamen kaldırmak için klasörü ayrıca sil.

## Kullanım

- `Alt+Boşluk`: pano geçmişi penceresini aç/kapat (KDE Plasma kurulumundan sonra).
- Arama kutusu: pano kayıtlarında, uygulamalarda ve taranan klasörlerde arama yap.
- Dosya sonucunu seç: sağ panelde görsel veya metin önizlemesi gösterilir; dosyayı açmak için Enter'a bas.
- Enter: pano kaydını kopyala; uygulamayı başlat; dosyayı varsayılan uygulamayla aç.
- Bağlantılar ve görseller otomatik kategorilenir. Pano kaydına sağ tık: kategoriyi değiştir, sabitle, kopyala veya sil.
- `+` veya `Ctrl+Shift+N`: yeni pano kategorisi ekle.
- Kategori düğmesine sağ tıkla: regex kuralı ekle/düzenle, adı değiştir veya kategoriyi sil. Örnek: `^INV-\d+` ile `INV-` ardından rakam gelen metinleri eşleştir. Regex kuralları büyük/küçük harfe duyarsızdır; birden çok kural eşleşirse kategori sırasındaki ilk kural kullanılır. Silinen kategorideki kayıtlar başka bir kategoriye taşınır.
- Metin pano kaydına sağ tıkla ve `Metni düzenle` seçeneğini kullan; metin aynı ekranda açılır. Kaydedilen içeriği Enter ile panoya kopyalayabilirsin.
- Escape: pencereyi gizle. Tepsi simgesinden yeniden açabilir veya uygulamadan çıkabilirsin.
- `./launch.sh` zaten açıkken tekrar çalışırsa yeni örnek başlatmaz; var olan pencereyi öne getirir.
- `Ctrl+1/2/3`: pano, uygulama veya dosya görünümüne geç; uygulama ve dosyalar yalnızca arama yazınca listelenir.
- Dosya araması yazmayı bıraktıktan 450 ms sonra `/` altında başlar; gizli dosya ve klasör adlarını ve yollarını büyük/küçük harfe duyarsız eşleştirir, dosya içeriğinde arama yapmaz. `/proc`, `/sys`, `/dev` ve `/run` taranmaz; erişilemeyen konumlar atlanır. Sonuçlar tarama sürerken gösterilir ve aşağı kaydırdıkça 150'şer yüklenir. Yeni arama yazarken önceki tarama durdurulur.
- Arama sonuçlarını `Tümü`, `Dosya`, `Görsel` ve `Klasör` türlerine göre filtrele; genel görünümde `Uygulama` filtresi de bulunur. Görsel filtresi yaygın resim uzantılarını ve panodaki görselleri içerir.
- `Ctrl+K`: aramaya dön; `Alt+←/→`: kategoriler arasında geç; `Ctrl+Shift+P`: seçili kaydı sabitle; `Ctrl+Shift+M`: kategorisini değiştir; `Delete`: kaydı sil; `Ctrl+Shift+Delete`: sabitlenmemiş geçmişi temizle.

Pano geçmişi `~/.local/state/komuta/clipboard.db` altında tutulur. Metinler ve dosya yolları 200 KB, tek dosya listesi 100 öğe, görseller öğe başına 8 MB ve toplam 100 MB ile sınırlıdır; sınırı aşan yeni içerik kaydedilmez. Dosyaların kendisi kopyalanmaz, yalnızca yolları saklanır; dosya taşınır veya silinirse geçmişteki kopyası da kullanılamaz. Görseller listede küçük önizlemeyle gösterilir. En yeni 200 normal kayıt ve en fazla 50 sabitlenmiş kayıt tutulur. Uygulama pano metin ve görsellerini diske yazar; gizli bilgi içeren kopyalamalara dikkat et. Geçmişi arayüzden temizleyebilirsin.

## Geliştirme

```bash
python3 app.py
```

Uygulama ve dosya listeleri açılışta arka planda taranır. Pano araması anlık, yerel ve ağ gerektirmez.
