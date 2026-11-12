<!-- Türkçe ya da İngilizce yazabilirsiniz. / Turkish or English is fine. -->

## Özet / Summary

<!-- Ne değişiyor ve neden? Bağlı issue: Fixes #… / What changes and why? Linked issue: Fixes #… -->

## Kontrol listesi / Checklist

- [ ] Testler eklendi veya güncellendi; `python tests/run_all.py` geçiyor / Tests added or updated; `python tests/run_all.py` passes
- [ ] `ruff check .` ve `mypy` temiz / `ruff check .` and `mypy` are clean
- [ ] Mevcut `config.json` dosyaları değişiklik gerekmeden açılıyor (yeni alanların varsayılanı var) / Existing `config.json` files still load unchanged (new fields have defaults)
- [ ] `requirements.txt` değiştiyse `python tools/refresh_wheels.py` ve `python tools/refresh_locks.py` çalıştırıldı / If `requirements.txt` changed, `python tools/refresh_wheels.py` and `python tools/refresh_locks.py` were run
- [ ] Gerekirse README (TR + EN) ve CHANGELOG güncellendi / README (TR + EN) and CHANGELOG updated where needed
- [ ] Arayüzdeki yeni metinler `_()` ile sarıldı ve `app/locale/tr.json`'a eklendi / New UI texts are wrapped in `_()` and added to `app/locale/tr.json`
- [ ] Arayüz değiştiyse önce/sonra ekran görüntüsü eklendi / UI changes come with before / after screenshots
- [ ] REST API ve CLI yalnızca büyüyor: alan, uç nokta ya da bayrak kaldırılmadı veya anlamı değişmedi / The REST API and the CLI only grow: no field, endpoint or flag removed or changed in meaning
- [ ] Yeni bir route `tests/test_route_access.py`'de bir erişim sınıfına sahip / A new route has an access class in `tests/test_route_access.py`
- [ ] Yeni bir kişisel veri saklıyor ya da host dışına gönderiyorsa (ya da saklama süresini değiştiriyorsa) `docs/PRIVACY.md` güncellendi / If it stores or sends new personal data (or changes how long it is kept), `docs/PRIVACY.md` was updated
- [ ] İlk katkıysa CLA imzalandı / CLA signed (first contribution)
