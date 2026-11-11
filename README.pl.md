🌐 [English](README.md) | **Polski** | [Українська](README.uk.md)

---

# Dedup sorter

![Dedup Sorter](docs/assets/dedup_sorter.jpg)

Narzędzie do porządkowania archiwum zdjęć i filmów: detekcja duplikatów dokładnych,
rozpoznawanie serii i wizualnie podobnych zdjęć, grupowanie w wyjazdy, triaż skrzynki wejściowej,
oraz kompresja filmów zajmujących wielokrotnie za dużo miejsca.

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy_Me_A_Coffee-FFDD00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/mbohdan)

**Domyślnie nic nie jest usuwane.** Narzędzie działa w trybie dry-run — dopóki
nie poprosisz wprost, wyłącznie raportuje, co proponuje. Nawet zastosowanie
zmian nie kasuje plików z dysku: przenosi je do kosza `!REMOVE` z manifestem,
który da się w całości cofnąć. Trwałe skasowanie wykonujesz zawsze ręcznie.

**Wyłącznie Windows 10/11.** Narzędzie korzysta z API Win32 (długie ścieżki, junctions, twarde linki); Linux i macOS nie są obsługiwane.

**Gdzie umieścić projekt?**
Katalog `.dedup` z pobranym kodem repozytorium musi znajdować się **bezpośrednio w głównym katalogu Twojego archiwum**, na którym będziesz operować. Narzędzie przetwarza folder nadrzędny wobec siebie.
Przy każdym uruchomieniu narzędzie tworzy w archiwum brakujące specjalne katalogi:
- `!NEW` – skrzynka wejściowa na nowo zrzucone zdjęcia do posortowania
- `!MOVES` – historia manifestów i kopii operacji
- `!REMOVE` – bezpieczny kosz, do którego trafiają odrzucone duplikaty i pliki oznaczone do usunięcia w interfejsie przeglądu (nigdy nie są trwale usuwane przez program)

Skrypty startowe `run.cmd` / `run.ps1` / `run.sh` są bardziej rygorystyczne: przerywają pracę, jeśli `!NEW` jeszcze nie istnieje, więc utwórz go wcześniej (krok 4 poniżej).

## Szybki start

Wymagane: Windows 10/11, Python 3.10 lub nowszy oraz Git.

> **Instalacja Pythona w systemie Windows:**
> Jeśli nie masz jeszcze zainstalowanego Pythona:
> 1. Pobierz instalator ze strony [python.org/downloads](https://www.python.org/downloads/) lub uruchom w PowerShellu: `winget install Python.Python.3.12`
> 2. **Ważne:** W oknie instalatora koniecznie zaznacz opcję **"Add python.exe to PATH"** przed kliknięciem Install.
> 3. Sprawdź poprawność instalacji w nowym oknie konsoli: `python --version`

1. Przejdź do folderu swojego archiwum (tego, które chcesz uporządkować):
   ```bat
   cd D:\MojeArchiwum
   ```
2. Sklonuj repozytorium do podfolderu `.dedup` i zainstaluj zależności:
   ```bat
   git clone https://github.com/bogdan580/dedup-sorter.git .dedup
   cd .dedup
   python -m pip install -r requirements.txt
   ```
3. Opcjonalnie: skopiuj szablon konfiguracji i edytuj go (bez `config.json` obowiązują wartości domyślne):
   ```bat
   copy config.example.json config.json
   ```
4. Opcjonalnie, dla triażu skrzynki wejściowej: utwórz folder `!NEW` w katalogu głównym archiwum (i wrzucaj tam nowe zdjęcia):
   ```bat
   mkdir ..\!NEW
   ```
5. Kliknij dwukrotnie `run.cmd` (albo uruchom `.\run.ps1` w PowerShellu lub `./run.sh` w Git Bash). Interfejs przeglądu otworzy się w przeglądarce.

**Pierwsze uruchomienie pobiera ffmpeg** (~200 MB, weryfikacja SHA-256) potrzebny do kompresji wideo. To jedyny dostęp do sieci w projekcie; kolejne uruchomienia działają w pełni offline, a sam `dedup.py` nigdy nie łączy się z internetem. Jeśli pobieranie się nie powiedzie, interfejs przeglądu i tak się uruchomi, tylko bez kompresji. Szczegóły: [dołączony plik binarny ffmpeg](docs/technical/components/compression-mechanism.md#bundled-ffmpeg-binary).

Polecenia CLI uruchamiaj z katalogu głównego archiwum (a nie z wnętrza `.dedup`), bo względne `--roots` są rozwiązywane względem bieżącego katalogu:

```bat
cd D:\MojeArchiwum
python .dedup\dedup.py --review
```

| Polecenie | Co robi |
|---|---|
| `python .dedup\dedup.py` | skan całego archiwum, bez zmian na dysku |
| `python .dedup\dedup.py --review` | przegląd w interfejsie webowym — tu podejmuje się decyzje |
| `python .dedup\dedup.py --apply` | przenosi odrzucone duplikaty do kosza |
| `python .dedup\dedup.py --restore MANIFEST` | cofa całą paczkę operacji |
| `python .dedup\dedup.py --inbox` | triaż skrzynki `!NEW`, raport HTML |
| `python .dedup\dedup.py --compress` | wskazuje filmy warte przekodowania, bez zmian na dysku |

![Story Book](docs/assets/story_book.jpg)

## Dokumentacja

Narzędzie posiada obszerną architekturę i zestaw reguł biznesowych. Pełna dokumentacja (w języku angielskim) znajduje się w katalogu [`docs/`](docs/index.md) — to jedyne źródło prawdy.

| Szukasz | Idź do |
|---|---|
| Czy to może skasować moje zdjęcia | [gwarancje bezpieczeństwa](docs/business/guarantees.md) |
| Jak zainstalować projekt | [instalacja](docs/business/instructions/installation.md) |
| Co robi dana flaga | [referencja wiersza poleceń](docs/business/instructions/CLI.md) |
| Typowe scenariusze | [scenariusze pracy](docs/business/instructions/workflows.md) |
| Który egzemplarz zostaje i dlaczego | [wybór wersji](docs/business/rules/version-selection.md) |
| Który film warto przekodować | [reguły kompresji](docs/business/rules/compression.md) |
| Coś nie działa (znane błędy) | [znane ograniczenia](docs/business/instructions/limitations.md) |
| Jak to jest zbudowane pod maską | [architektura](docs/technical/architecture.md) |

## Kompresja Wideo

Kompresja korzysta z ffmpeg, który nie jest częścią repozytorium: skrypty startowe pobierają przypiętą, zweryfikowaną sumą kontrolną wersję przy pierwszym uruchomieniu (patrz Szybki start), więc ręczna instalacja nie jest potrzebna. Szczegóły: [mechanizm kompresji](docs/technical/components/compression-mechanism.md).

## Testy i Rozwój

Dla osób chcących współtworzyć kod (w tym agentów AI — patrz [AGENTS.md](AGENTS.md) i [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Konwencje, styl kodowania i stan zestawu: [testy i konwencje](docs/technical/development/conventions-and-tests.md). Kod stara się nie zawierać obszernych komentarzy — szczegóły logiki biznesowej lądują w dokumentacji, a z kodu prowadzą do nich wyłącznie "kotwice" (np. `# See: docs/business/rules/...`).

## Licencja

Projekt udostępniany na warunkach licencji [MIT](LICENSE). ffmpeg jest pobierany osobno (GPLv3, buildy BtbN) i nie jest częścią tego repozytorium.
