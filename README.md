# clipear — clips verticales de tus VODs de Kick

## 1. Instalar (una sola vez)
- **Python 3.9+** → https://www.python.org/downloads/ (tildá "Add python.exe to PATH")
- **ffmpeg** → Windows: `winget install Gyan.FFmpeg` (cerrá y abrí la terminal después)
- **Librerías:** `pip install -r requirements.txt`

El script revisa todo esto al arrancar y te dice qué falta.

## 2. Verificar el recorte de tu cámara (recomendado la primera vez)
    python clipear.py --url <LINK_DEL_VOD> --probar-layout 600
Genera `clips/layout_original.png` (rojo = facecam, verde = recorte del contenido) y una
captura de cada formato. Si no calza, corregí `facecam` en `config.json`.

## 3. Sacar clips
    python clipear.py --url <LINK_DEL_VOD> --cantidad 10 --formato split

Si Kick bloquea la descarga, te pide la ruta de un archivo local. También podés pasarlo directo:

    python clipear.py --archivo vod.mp4 --url <LINK_DEL_VOD> --cantidad 10

Antes de renderizar te muestra la lista y espera tu OK. Comandos:
`ok`, `quitar 3 7`, `formato 2 cam`, `formato todos full`, `titulo 4 Nuevo título`, `mover 5 +3 -2`, `editar`, `salir`.

## Opciones útiles
| opción | qué hace |
|---|---|
| `--modelo medium` | transcripción más precisa (más lenta; por defecto `small`) |
| `--min 20 --max 60` | duración de los clips |
| `--sin-chat` | no bajar el chat de Kick |
| `--desde-candidatos` | retomar la lista guardada sin volver a analizar |
| `--si` | renderizar sin preguntar |

Lo pesado (descarga, transcripción, chat) se guarda en `trabajo/<id>/`: si lo volvés a correr, no se repite.

## Salida
`clips/clip_01_<titulo>.mp4` … y `clips/clips.csv` (separado por `;` para que Excel en español lo abra bien).
