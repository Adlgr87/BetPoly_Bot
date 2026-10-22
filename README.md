# BetPoly_Bot 🎯

Bot de Telegram para predicciones y apuestas entre usuarios. Los usuarios crean predicciones, apuestan puntos en resultados y compiten en un ranking global.

---

## 📊 Product Backlog — Análisis de Mercado → Features de Telegram

> Análisis de necesidades del usuario en el mercado de predicciones sociales y apuestas informales entre amigos/comunidades de Telegram.

| ID | Necesidad del Usuario (Análisis) | Feature Propuesta | Implementación en Telegram (UX) | Prioridad |
| :--- | :--- | :--- | :--- | :--- |
| F001 | Los usuarios no saben qué puede hacer el bot ni cómo empezar; la fricción inicial es alta y abandona antes de experimentar valor. | **Onboarding Guiado**: Presentar el bot, registrar al usuario y mostrar las acciones principales en un solo flujo. | Comando `/start` que envía un mensaje de bienvenida con un `InlineKeyboardMarkup` de 2 botones: "➕ Crear Predicción" y "📋 Ver Predicciones Activas". El registro ocurre de forma silenciosa en base de datos. | **Alta** |
| F002 | Los usuarios quieren proponer eventos inciertos para que su comunidad opine/apueste, pero sin herramientas nativas de Telegram para ello. | **Crear Predicción**: Permitir al usuario definir una pregunta con múltiples opciones y una fecha de cierre. | Comando `/crear` que inicia un flujo conversacional por pasos (`ConversationHandler`): (1) pide el título, (2) pide las opciones separadas por comas, (3) pide la fecha de cierre. Confirmación final con `InlineKeyboardMarkup` ("✅ Confirmar" / "❌ Cancelar"). | **Alta** |
| F003 | Los usuarios necesitan ver qué predicciones están abiertas de forma clara y rápida sin salir de Telegram. | **Ver Predicciones Activas**: Listar todas las predicciones abiertas con estado en tiempo real. | Comando `/predicciones` que envía una lista paginada de predicciones abiertas. Cada ítem tiene botones inline: "🎲 Apostar" (por predicción) y botones de navegación "⬅️ Anterior / ➡️ Siguiente". | **Alta** |
| F004 | Los usuarios quieren apostar puntos a una opción concreta de forma intuitiva, sin comandos complejos. | **Apostar en una Predicción**: Seleccionar opción y cantidad de puntos a apostar. | Al pulsar "🎲 Apostar" se muestra un `InlineKeyboardMarkup` con las opciones disponibles. Tras elegir la opción, el bot pide la cantidad de puntos por mensaje de texto. Confirmación con resumen y botón "✅ Confirmar apuesta". | **Alta** |
| F005 | Los usuarios quieren ver su historial de apuestas, saldo de puntos y tasa de acierto para medir su rendimiento. | **Mis Apuestas / Perfil**: Vista personal con estadísticas de rendimiento. | Comando `/misbets` que envía una tarjeta de perfil con: saldo de puntos 💰, apuestas ganadas/perdidas/pendientes y porcentaje de acierto. Botón inline "📜 Ver historial completo" para listar apuestas anteriores. | **Alta** |
| F006 | La competición entre usuarios genera retención; sin un ranking visible, los usuarios pierden motivación a largo plazo. | **Leaderboard / Ranking Global**: Tabla de los mejores apostadores por puntos acumulados. | Comando `/ranking` que muestra el top-10 con medallas 🥇🥈🥉 y la posición del usuario solicitante destacada. Botón inline "🔄 Actualizar" para refrescar datos. | **Media** |
| F007 | Los usuarios pierden predicciones porque no recuerdan que están abiertas; la falta de recordatorios reduce la participación. | **Alertas y Notificaciones Push**: Avisar a los participantes cuando una predicción está a punto de cerrar o ha sido resuelta. | Mensajes proactivos enviados por el bot (job scheduler) 24 h y 1 h antes del cierre. Al resolver una predicción, el bot notifica individualmente a cada apostador con su resultado y puntos ganados/perdidos. | **Media** |
| F008 | Los administradores necesitan poder resolver predicciones (declarar el resultado correcto) sin intervención técnica. | **Panel de Administrador**: Resolver predicciones y gestionar el estado del bot. | Comando `/admin` restringido por `user_id` en la config. Lista de predicciones pendientes de resolución con `InlineKeyboardMarkup`: seleccionar predicción → seleccionar opción ganadora → confirmar. El bot calcula y distribuye puntos automáticamente. | **Alta** |
| F009 | Los usuarios nuevos y recurrentes necesitan un punto de consulta rápida sobre los comandos disponibles. | **Ayuda Contextual**: Mostrar comandos disponibles y descripción breve. | Comando `/help` con lista de comandos y emojis, más un botón inline "📖 Cómo funciona" que envía un mensaje educativo con el flujo principal del bot. | **Media** |
| F010 | En grupos de Telegram, los usuarios quieren crear predicciones directamente sin salir del chat grupal. | **Modo Grupo**: Habilitar el bot en grupos y canales para crear/votar predicciones colectivas. | Soporte del bot para ser añadido a grupos. Los comandos `/crear` y `/predicciones` funcionan en contexto grupal. Las predicciones creadas en un grupo quedan asociadas a ese `chat_id`, visibles solo para los miembros del grupo. | **Baja** |

---

## 🏗️ Recomendación de Arquitectura

### Decisión: `InlineKeyboardMarkup` como paradigma principal

Para **BetPoly_Bot**, la arquitectura recomendada es un **flujo guiado completamente por `InlineKeyboardMarkup`** en lugar de un `MenuButton` (menú persistente de comandos).

**Razones:**

1. **Flujos multi-paso** — Crear una predicción, seleccionar opciones y confirmar una apuesta requiere conversaciones de varios pasos (`ConversationHandler`). Los botones inline mantienen el contexto dentro de cada mensaje y permiten editar el mensaje original (`.edit_message_text`) para una experiencia fluida sin saturar el chat.

2. **Feedback inmediato** — Los `InlineKeyboardButton` con `callback_data` permiten actualizar el estado del mensaje (ej. deshabilitar el botón de apuesta una vez votado) sin necesidad de comandos adicionales.

3. **Experiencia sin fricción** — El usuario nunca necesita escribir comandos manualmente después del `/start`; toda la navegación ocurre a través de botones, reduciendo la curva de aprendizaje.

4. **El `MenuButton` como complemento** — Se configura un `MenuButton` con los comandos `/start`, `/predicciones`, `/ranking`, `/misbets` y `/help` como acceso rápido desde el menú de comandos de Telegram, pero no como flujo principal.

### Stack Tecnológico

| Componente | Tecnología |
| :--- | :--- |
| Lenguaje | Python 3.11+ |
| Framework Bot | `python-telegram-bot` v21 (async) |
| Base de Datos | SQLite (via `aiosqlite`) |
| Scheduler | `APScheduler` (alertas push) |
| Configuración | Variables de entorno (`.env`) |

---

## 🚀 Instalación y Ejecución

```bash
# 1. Clonar el repositorio
git clone https://github.com/Adlgr87/BetPoly_Bot.git
cd BetPoly_Bot

# 2. Crear entorno virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar variables de entorno
cp .env.example .env
# Editar .env y añadir BOT_TOKEN y ADMIN_IDS

# 5. Ejecutar el bot
python main.py
```

## 📁 Estructura del Proyecto

```
BetPoly_Bot/
├── bot/
│   ├── __init__.py
│   ├── database.py          # Capa de persistencia SQLite
│   ├── keyboards.py         # Helpers para InlineKeyboardMarkup
│   └── handlers/
│       ├── __init__.py
│       ├── start.py         # /start — onboarding
│       ├── predictions.py   # /crear, /predicciones, /apuesta
│       ├── stats.py         # /misbets, /ranking
│       ├── admin.py         # /admin — panel de administrador
│       └── help.py          # /help
├── main.py                  # Punto de entrada del bot
├── config.py                # Configuración y variables de entorno
├── requirements.txt
├── .env.example
└── README.md
```
