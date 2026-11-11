"""Group names as an admin types them into ser2net and as a directory or identity
provider stores them — Turkish dotted/dotless I in either letter case, decomposed
(NFD) text, no-break spaces. Shared by test_ldap.py and test_oidc.py."""
import unicodedata

# (typed in ser2net, as the directory / provider has it): the same group
SAME_GROUP = [
    ("Bilgi İşlem", "BİLGİ İŞLEM"),
    ("bilgi işlem", "Bilgi İşlem"),
    ("Işık Odası", "IŞIK ODASI"),
    ("IŞIK ODASI", "ışık odası"),
    ("Kalite", "KALİTE"),
    ("Bakım", "BAKIM"),
    ("Teknik", "TEKNİK"),
    ("Yönetim", "YÖNETİM"),
    ("İnsan Kaynakları", "insan kaynakları"),
    ("Üretim Şefleri", "ÜRETİM ŞEFLERİ"),
    (unicodedata.normalize("NFD", "Bilgi İşlem"), "BİLGİ İŞLEM"),   # decomposed input
    ("Bilgi İşlem", unicodedata.normalize("NFD", "BİLGİ İŞLEM")),
    ("Bilgi\u00a0İşlem", "BİLGİ İŞLEM"),         # no-break space inside the name
    ("Bilgi İşlem", "BİLGİ\u202fİŞLEM"),         # narrow no-break space
    ("  Bilgi   İşlem ", "BİLGİ İŞLEM"),         # extra spaces
]

# (typed, stored): NOT the same group — part of a name, or another name
OTHER_GROUP = [
    ("Bilgi", "Bilgi İşlem"),
    ("İşlem", "BİLGİ İŞLEM"),
    ("Kalite", "Kalite Kontrol"),
    ("Otomasyon", "Otomasyon Ekibi"),
    ("Bakım", "Bakım Onarım"),
    ("ser2net-admins", "ｓｅｒ２ｎｅｔ－ａｄｍｉｎｓ"),   # fullwidth letters are other letters
]
