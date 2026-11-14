# Restockr

**Creado por Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99))**

Vigilante de reposición **gratis**. Corre en [GitHub Actions](https://docs.github.com/es/actions) (computadoras que GitHub presta sin costo), revisa páginas de productos cada 1 a 2 minutos y te avisa por [Telegram](https://telegram.org/) cuando hay stock, cambia la fecha o, en Disney, se abre la fila virtual. Si quieres, también puede avisarte por [ntfy](https://ntfy.sh/).

> **English:** Free restock watcher that runs on GitHub Actions and alerts you on Telegram (ntfy optional). Full guide: [README_EN.md](README_EN.md). Created by Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99)).

## Qué hace

- Revisa una o más páginas de producto, de día y de noche, aunque tu computadora esté apagada.
- Te manda una **tarjeta en Telegram** con foto, estado, precio, fecha prevista (si la página la muestra) y un botón **🛒 Ver producto**.
- Opcionalmente envía el mismo aviso a ntfy.
- Todos los días, a las **9:00 hora de México** (America/Mexico_City), te manda un mensaje de que sigue vivo, siempre que el producto principal no esté en stock.
- Se apaga solo el **21 de noviembre de 2026** (fecha fija en el código; más abajo se explica cómo cambiarla).

Está pensado sobre todo para **OpenAI Supply Co.** y **Disney Store**. En otras tiendas puede funcionar si la página muestra textos o botones típicos de “agotado” o “añadir al carrito”, pero no está garantizado.

## Cómo funciona

```
GitHub Actions
  (cada 20 minutos empieza un turno de unas 5 h 40 min)
        │
        ▼
  El programa abre cada página del producto
  (una petición HTTPS que se hace pasar por Chrome)
  y la vuelve a revisar cada 60–120 segundos
        │
        ├─ Hay stock, cambió la fecha, se abrió la fila
        │  virtual, se agotó de nuevo o no puede leer la página
        │         ▼
        │    Telegram  (+ ntfy, si lo configuraste)
        │
        └─ Una vez al día, a las 9:00 hora de México:
              «☁️ Vigilante en la nube activo»
```

No usa inteligencia artificial ni gasta tokens: es un programa que corre solo. Las claves del bot se guardan como **secretos de GitHub**, no en el código.

## Avisos que puedes recibir

| Aviso | Qué significa |
|---|---|
| ☁️ Restockr en la nube: activo | Arranque o prueba: pudo (o no) leer cada producto |
| 🟢 ¡Ya hay stock! | El producto está disponible. Se repite cada 2 minutos, hasta 10 veces |
| 📅 Cambió la fecha de restock | La página muestra una fecha prevista distinta a la anterior |
| ⚪ Se agotó de nuevo | Estaba disponible y volvió a agotarse |
| 🚦 ¡Abrió la fila virtual! Entra ya | La página redirigió a una sala de espera `queue.*` (como la de Disney). Hasta 3 avisos, cada 3 minutos |
| ⏰ Hoy es el restock | Solo el producto marcado como principal, el 19 de noviembre de 2026 a partir de las 7:00 hora de México |
| ☁️ Vigilante en la nube activo | Latido diario (si el producto principal no está en stock) |
| ⚠️ No pude leer el producto N veces seguidas | Falló 5 veces seguidas (posible bloqueo). Sigue reintentando, con más espera |
| ✅ Vigilante recuperado | Volvió a leer la página después de ese bloqueo |

Si un producto tiene `"is_test": true` en la configuración, sus avisos salen con el prefijo **🧪 Prueba**.

## Requisitos

- Una cuenta en [GitHub](https://github.com/signup). No hace falta tarjeta. El repositorio puede ser **público** (los minutos de Actions suelen ser ilimitados en el plan gratis) o **privado** (tienen un cupo mensual de minutos).
- Una cuenta en [Telegram](https://telegram.org/).
- Unas 15 minutos para el armado inicial.

## Cómo montarlo (paso a paso)

### 1. Crea tu propia copia del proyecto

1. Entra a [https://github.com/Moljesdev99/restockr](https://github.com/Moljesdev99/restockr) (inicia sesión en GitHub).
2. Pulsa el botón verde **Use this template** → **Create a new repository**.
   - Si no ves ese botón, pulsa **Fork**.
3. Ponle un nombre (por ejemplo `restockr`), déjalo **Public** si quieres aprovechar los minutos gratis, y crea el repositorio.

Así el código queda en *tu* cuenta. Los avisos te llegarán a ti, no al autor.

### 2. Crea un bot en Telegram

1. En Telegram, abre [@BotFather](https://t.me/BotFather) y envía `/newbot`.
2. Elige un nombre (el que se ve en el chat, por ejemplo `Restockr`) y un nombre de usuario que termine en `bot`.
3. BotFather te da un **token** (una clave larga). Cópiala y guárdala en un lugar seguro. **No la compartas ni la subas al repositorio.**
4. Abre tu bot nuevo (el enlace que te dio BotFather) y pulsa **Start** o envía `/start`. Sin este paso, el bot no puede escribirte.

### 3. Averigua tu chat id

1. Envíale cualquier mensaje a tu bot (si aún no lo hiciste, `/start` basta).
2. En el navegador, abre esta dirección, reemplazando `TU_TOKEN` por el token del paso anterior:

   `https://api.telegram.org/botTU_TOKEN/getUpdates`

3. En el texto que aparece, busca algo como `"chat":{"id": 123456789`. Ese número es tu **chat id**.
   - En un chat personal suele ser un número positivo.
   - En un grupo suele ser un número negativo. Si usas un grupo, invita al bot y escribe un mensaje ahí antes de abrir el enlace.

Si la página muestra `"ok":true,"result":[]`, vuelve a enviarle un mensaje al bot y recarga.

### 4. (Opcional) Prepara ntfy

ntfy es un respaldo: te llega un aviso en el teléfono aunque Telegram falle.

1. Inventa un nombre de tema **largo y difícil de adivinar**, como una contraseña. Quien conozca ese nombre puede leer tus avisos en ntfy.sh.
2. Suscríbete a ese tema en la [app de ntfy](https://ntfy.sh/) o en `https://ntfy.sh/TU_TEMA`.
3. Más adelante lo guardarás como el secreto `NTFY_TOPIC`. Si no lo configuras, el vigilante solo usa Telegram.

### 5. Guarda las claves como secretos de GitHub

En *tu* repositorio:

1. Ve a **Settings** → **Secrets and variables** → **Actions**.
2. Pulsa **New repository secret** y crea estos dos (los nombres tienen que coincidir exactamente):

   | Nombre | Valor |
   |---|---|
   | `TG_TOKEN` | El token que te dio BotFather |
   | `TG_CHAT` | Tu chat id (solo el número) |

3. Si usas ntfy, crea también `NTFY_TOPIC` con el nombre de tu tema.

Nunca pongas estas claves en `config.json`, en el README ni en un comentario.

### 6. Pon tus productos en `config.json`

En tu repositorio, abre `config.json` → el lápiz de editar. Puedes partir de [`config.example.json`](config.example.json). Deja una lista como esta:

```json
{
  "products": [
    {
      "id": "mi-producto",
      "name": "Nombre completo del producto",
      "short_name": "el producto",
      "url": "https://tienda.ejemplo.com/producto",
      "emoji": "🎮",
      "is_test": false,
      "primary": true
    }
  ]
}
```

| Campo | Obligatorio | Qué es |
|---|---|---|
| `id` | Sí | Identificador corto, sin espacios, único (ejemplo: `chromatic`) |
| `url` | Sí | Enlace a la página del producto |
| `name` | No | Nombre que aparece en la tarjeta. Si lo omites, se usa el `id` |
| `short_name` | No | Se usa en «¡Ya hay stock de …!». Si lo omites, se usa `name` |
| `emoji` | No | Emoji de la tarjeta. Por defecto 🎮 |
| `is_test` | No | `true` marca los avisos como 🧪 Prueba. Por defecto `false` |
| `primary` | No | `true` en **un** producto: recibe el latido diario y el aviso del 19 de noviembre. Si ninguno lo tiene, se usa el primero de la lista |

Puedes añadir más objetos dentro de `products`. Luego pulsa **Commit changes**.

En este repositorio, `config.json` trae de ejemplo los dos productos con los que se probó el vigilante (Chromatic Codex y el sable de Disney). Cámbialos por los tuyos.

### 7. Activa GitHub Actions y haz una prueba

1. En tu repositorio, abre la pestaña **Actions**.
2. Si GitHub pide confirmar que confías en los flujos de trabajo, acepta.
3. En la lista de la izquierda, entra a **Restockr**.
4. Pulsa **Run workflow**, elige `test` y confirma.

En uno o dos minutos deberías recibir en Telegram **☁️ Restockr en la nube: activo**, con el estado de cada producto. Si llega, vuelve a **Run workflow**, esta vez con `watch`, para dejarlo vigilando. El horario programado también lo irá arrancando solo.

## Añadir o quitar productos

Edita `config.json`, deja o borra entradas en `products`, y haz **Commit**. El cambio se aplica en la siguiente corrida.

No hace falta tocar `state.seed.json`. Ese archivo solo sirve como estado inicial la primera vez (o si GitHub pierde la memoria entre corridas). Si cambias de productos, puedes dejarlo o borrarlo; los productos nuevos empiezan con estado en blanco.

## Personalizar la frecuencia y las fechas

Estos valores están en el código, no en `config.json`. Cámbialos con un commit si los necesitas distintos:

| Qué | Dónde | Valor actual |
|---|---|---|
| Pausa entre revisiones | `CHECK_MIN_S` y `CHECK_MAX_S` en `restockr_core.py` | 60 a 120 segundos |
| Cada cuánto GitHub inicia un turno | `cron` en `.github/workflows/restockr.yml` | minutos 7, 27 y 47 de cada hora |
| Duración de cada turno | `RUN_MINUTES` en el flujo de trabajo | 340 minutos (~5 h 40 min) |
| Latido diario | `HEARTBEAT_HOUR` y `HEARTBEAT_MINUTE` en `restockr_core.py` | 9:00, zona `America/Mexico_City` |
| Aviso «Hoy es el restock» | `NOV19` en `restockr_core.py` | 19 de noviembre de 2026, 7:00 hora de México, solo el producto principal |
| Apagado automático | `END_AT` en `restockr_core.py` | 21 de noviembre de 2026, 23:59 hora de México |

El cron de GitHub **no** revisa el producto por sí solo: solo arranca un turno. Dentro del turno, el programa revisa cada 1 a 2 minutos. Si un turno sigue en marcha, el siguiente espera su lugar (no se cancelan entre sí). Si quieres que siga después del 21 de noviembre de 2026, cambia `END_AT`.

## Cómo apagarlo

En tu repositorio: **Actions** → **Restockr** → el menú **…** → **Disable workflow**.

## Solución de problemas

**No me llegó el mensaje de prueba**
- Confirma que los secretos se llaman exactamente `TG_TOKEN` y `TG_CHAT`.
- Confirma que le enviaste `/start` a tu bot.
- Abre **Actions** → la corrida más reciente → el paso **Watch** y lee si aparece un error (por ejemplo, “Missing TG_TOKEN / TG_CHAT”).

**El mensaje dice que no pudo leer una página**
- La tienda puede estar bloqueando al vigilante (Cloudflare u otra protección).
- La página puede depender de mucho JavaScript, o no usar textos/botones que el programa reconoce.
- Prueba primero con una página de OpenAI Supply o de Disney Store, que son las que ya se comprobaron.

**El aviso de stock llegó tarde**
- El cron de GitHub Actions a veces se atrasa varios minutos.
- Si un turno falla o termina antes de tiempo, puede haber un hueco hasta el siguiente.

**Me llegan avisos repetidos**
- Suele pasar si también corre otra copia del vigilante (por ejemplo, en otra computadora o en otro repositorio).
- Si GitHub pierde la memoria entre corridas (`state.json`), algunos avisos pueden volver a salir una vez.

**ntfy no llega**
- El secreto `NTFY_TOPIC` es opcional: si no está, solo se usa Telegram.
- Revisa que el nombre del tema coincida exactamente y que estés suscrito a ese mismo tema.
- El plan gratis de ntfy.sh limita cuántos mensajes puedes recibir al día.

**Dejó de correr**
- Mira si el flujo está desactivado en la pestaña **Actions**.
- Si ya pasó la fecha de `END_AT`, el programa arranca y sale de inmediato. Cambia esa fecha si quieres que continúe.

## Limitaciones

- **Retrasos de GitHub Actions.** El horario `cron` no es exacto al segundo; GitHub puede atrasarlo.
- **Condiciones de uso de GitHub.** Actions está pensado sobre todo para proyectos de programación (pruebas, publicación de código, etc.). Usarlo como un servicio personal siempre encendido puede salirse de ese uso, y GitHub podría limitar o cortar el flujo. Úsalo bajo tu responsabilidad.
- **Protección contra bots.** Tiendas con Cloudflare, captchas u otras barreras pueden impedir la lectura. El programa avisa si falla 5 veces seguidas, y sigue intentándolo.
- **No es un monitor universal.** La detección está afinada para OpenAI Supply Co. y Disney Store (incluido el cuarto de espera Queue-it, cuando la dirección final empieza por `queue.`). En otras webs el resultado es incierto.
- **Fecha de corte incluida.** Esta copia se detiene el 21 de noviembre de 2026 hasta que cambies `END_AT`.
- **El estado vive en la caché de Actions.** No está en el repositorio (y no debe subirse). Si la caché se pierde, el vigilante vuelve a partir de `state.seed.json` o de un estado en blanco.

## Privacidad

- **Nunca subas** el token del bot, el chat id ni el tema de ntfy al repositorio, ni los escribas en issues o en el README.
- Guárdalos solo como secretos de GitHub (**Settings** → **Secrets and variables** → **Actions**).
- Si el repositorio es público, otras personas verán tu `config.json` (nombres y enlaces de productos). Las claves, no.
- El tema de ntfy funciona como una contraseña: elige uno largo y no lo publiques.
- El archivo [`.gitignore`](.gitignore) ignora `state.json`, registros y archivos típicos de claves locales (`.tg_token`, `.tg_chat`, `.env`).

## Licencia

[MIT](LICENSE). Copyright © 2026 Jesús Molina.

---

**Creado por Jesús Molina ([@Moljesdev99](https://github.com/Moljesdev99))**
