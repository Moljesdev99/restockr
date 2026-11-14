# Restockr en la nube (GitHub Actions)

Revisa la Chromatic Codex y el Ezra Lightsaber cada 1–2 minutos, las 24 h,
en computadoras de GitHub (gratis, sin tarjeta). Avisa al bot Restockr de
Telegram y a ntfy. Se apaga solo el 21 de noviembre.

## Opción fácil (recomendada)
1. Crea una cuenta en https://github.com/signup (correo + contraseña + código). No pide tarjeta.
2. Avísale a Julieta: ella abre GitHub en su navegador, tú inicias sesión ahí, y ella
   sube todo y guarda las claves sin mostrarlas (script `deploy.sh`).
3. En 1–2 minutos te llega a Telegram: «☁️ Restockr en la nube: activo».

## Opción manual (si prefieres hacerlo tú, ~15 min desde la Mac)
1. Cuenta en https://github.com/signup.
2. Botón **New** (repositorio) → nombre `restockr` → **Public** → marca "Add a README" → Create.
3. **Add file → Upload files** → arrastra: `restockr.py`, `restockr_core.py`,
   `config.json`, `requirements.txt`, `state.seed.json` → **Commit changes**.
4. **Add file → Create new file** → nombre exacto: `.github/workflows/restockr.yml`
   → pega el contenido de ese archivo → **Commit changes**.
5. **Settings → Secrets and variables → Actions → New repository secret**, tres veces:
   - `TG_TOKEN` = el token del bot Restockr (BotFather → /mybots → Restockr → API Token)
   - `TG_CHAT` = tu número de chat (te lo pasa Julieta)
   - `NTFY_TOPIC` = `jesus-gameboy-k1zkpyms`
6. Pestaña **Actions** → "Restockr" → **Run workflow** → `watch` → Run.
   Te llega «☁️ Restockr en la nube: activo» a Telegram.

## Qué vas a recibir
- 🟢 ¡Ya hay stock! (se repite cada 2 min, hasta 10 veces)
- 🚦 ¡Abrió la fila virtual! (Disney, el día del lanzamiento)
- ☁️ Vigilante en la nube activo — una vez al día, 12:00 hora Paraguay
- ⚠️ Si no puede leer una página 5 veces seguidas

## Para apagarlo
Actions → Restockr → botón «…» → **Disable workflow**.
