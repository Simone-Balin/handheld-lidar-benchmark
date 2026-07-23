# Inventario preparazione dati, benchmark LC3D 2026 (sito Fontaniva)

Data: 2026-07-08. Conversioni eseguite con CloudCompare 2.12.4 (Kyiv) da riga di comando, una alla volta, senza alcun subsampling: le nuvole di lavoro sono integre, come richiesto dalla regola di equita del benchmark. Verifica dei LAS prodotti con laspy (Python).

## Tabella riepilogativa conversioni e57, LAS

| Device | File sorgente | Nuvole nell'e57 | Punti totali | Bounding box (min; max, metri) | File LAS prodotto | Anomalie |
|---|---|---|---|---|---|---|
| Leica RTC360 (TLS, riferimento) | `_SCANSIONI\_scan\RTC360.e57` (353,6 MB) | 1 (scan node "Fontaniva 1-50") | 11.906.619 | (12,512; -9,249; -1,682) ; (18,559; -3,270; 0,883) | `RTC360.las` (295,2 MB) | Nessuna |
| Leica BLK2GO (handheld) | `_SCANSIONI\_scan\BLK2GO.e57` (42,3 MB) | 1 (scan node "Fontaniva 1-1") | 1.575.579 | (-0,454; 141,017; 6,068) ; (5,167; 146,872; 8,522) | `BLK2GO.las` (39,1 MB) | Punti molto inferiori agli altri device (vedi note) |
| iPhone 17 Pro (app SiteScape) | `_SCANSIONI\_scan\IPHONE17.e57` (344,6 MB) | 1 (scan node "Scan at 18.09") | 23.983.226 | (-2,893; -5,255; -1,462) ; (3,416; 3,818; 1,115) | `IPHONE17.las` (594,7 MB) | LAS piu grande dell'e57 sorgente (normale, formato non compresso) |

Estensioni delle bounding box (X per Y per Z, metri): RTC360 6,05 x 5,98 x 2,57; BLK2GO 5,62 x 5,86 x 2,45; IPHONE17 6,31 x 9,07 x 2,58. Le dimensioni della scena sono coerenti tra i tre device.

## Verifica dei LAS prodotti (laspy)

Tutti e tre i LAS si aprono correttamente con laspy. Versione LAS 1.2, point format 2 (XYZ, intensity, RGB e campi standard). Il numero di punti nell'header LAS coincide esattamente con quello letto da CloudCompare in fase di caricamento dell'e57 (nessuna perdita di punti). Anche la dimensione dei file e coerente con l'aritmetica del formato: 26 byte per punto piu 227 byte di header, verificata al byte per tutti e tre i file.

| File LAS | Punti (header) | Min (X; Y; Z) | Max (X; Y; Z) | Esito |
|---|---|---|---|---|
| `RTC360.las` | 11.906.619 | 12,512; -9,249; -1,682 | 18,559; -3,270; 0,883 | OK |
| `BLK2GO.las` | 1.575.579 | -0,454; 141,017; 6,068 | 5,167; 146,872; 8,522 | OK |
| `IPHONE17.las` | 23.983.226 | -2,893; -5,255; -1,462 | 3,416; 3,818; 1,115 | OK |

## Verifica ripetizioni iPhone (solo apertura, nessuna conversione)

File in `_SCANSIONI\_APP IPHONE 17 PRO\_others\`, formato PLY generato da SiteScape 1.0. Tutti e tre si aprono correttamente in CloudCompare (una nuvola per file).

| File PLY | Punti | Esito |
|---|---|---|
| `Scan at 18.09.ply` | 23.983.226 | OK |
| `Scan at 18.12.ply` | 23.974.142 | OK |
| `Scan at 18.15.ply` | 23.973.001 | OK |

Nota: `IPHONE17.e57` contiene lo scan node "Scan at 18.09" con lo stesso identico numero di punti della ripetizione `Scan at 18.09.ply`: l'e57 di lavoro deriva quindi dalla prima ripetizione delle 18.09.

## Note e anomalie

1. Sistemi di riferimento locali diversi: le tre bounding box non si sovrappongono (RTC360 attorno a X 12-19 m, BLK2GO con Y attorno a 141-147 m, IPHONE17 attorno all'origine). Ogni device esporta nel proprio frame locale: la registrazione delle nuvole sul riferimento e necessaria negli step successivi del piano, come previsto.
2. BLK2GO con 1,58 milioni di punti contro gli 11,9 (RTC360) e i 24,0 (iPhone): densita nativa dell'export via Autodesk ReCap, non un errore di conversione (il conteggio LAS coincide con quello dell'e57 sorgente). Da tenere presente nelle analisi di densita del benchmark.
3. `IPHONE17.las` (594,7 MB) e piu grande dell'e57 sorgente (344,6 MB): atteso, il LAS point format 2 non e compresso.
4. Dettaglio operativo CLI: con percorsi contenenti spazi, l'argomento di `-SAVE_CLOUDS FILE` va racchiuso tra virgolette doppie interne all'argomento stesso, altrimenti CloudCompare lo spezza in piu nomi file (errore "specified 3 file names, but there are 1 clouds" al primo tentativo, poi risolto).
5. Nessun subsampling e nessuna altra trasformazione applicata: solo lettura e57, merge (innocuo, una sola nuvola per file) ed esportazione LAS.

## Provenienza dei file sorgente (dai metadati e57)

| File | Generatore dichiarato nell'e57 |
|---|---|
| `RTC360.e57` | CloudCompare v2.12.4 (Kyiv) |
| `BLK2GO.e57` | Autodesk ReCap |
| `IPHONE17.e57` | SiteScape v1.8.3 (219) |

## Log

Un log per ciascuna conversione, piu i log delle verifiche di apertura dei PLY, tutti in questa cartella:

- `RTC360.log`, `BLK2GO.log`, `IPHONE17.log` (log CloudCompare delle conversioni; i file `*.stdout.txt` affiancati contengono anche l'elenco dei plugin caricati)
- `PLY_Scan_18.09_verifica.log`, `PLY_Scan_18.12_verifica.log`, `PLY_Scan_18.15_verifica.log` (verifiche di sola apertura)

Comando tipo utilizzato per le conversioni:

```
"C:\Program Files\CloudCompare\CloudCompare.exe" -SILENT -LOG_FILE "<out>.log" -AUTO_SAVE OFF
  -O "<input>.e57" -MERGE_CLOUDS -C_EXPORT_FMT LAS -SAVE_CLOUDS FILE "<output>.las"
```
