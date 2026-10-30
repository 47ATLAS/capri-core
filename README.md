# Capri — Fase 1: backend FastAPI

Backend del SDD del Proyecto Capri. Incluye orquestador, memoria intercambiable, psique W++, edición segura con Git, finanzas SQLite y telemetría Tasker. **No incluye interfaz visual, lectura de ventanas, control de Spotify, notificaciones nativas ni generación de voz.**

## Instalación y arranque

Requiere Python 3.11 o superior y Git. La entrega se verificó en Windows con Python 3.14. Las pruebas y el modo `dev/fake` no necesitan red, Ollama ni llaves de Gemini, Mem0 o ElevenLabs; la instalación de dependencias sí necesita acceso al índice de paquetes.

```powershell
git clone --branch fase-1-backend https://github.com/47ATLAS/capri-core.git
cd capri-core
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

En Linux/macOS, usar `python3`, `.venv/bin/python` y `cp .env.example .env`. Configurar **un valor propio y aleatorio para `CAPRI_API_KEY`** en `.env`; el valor del ejemplo no es seguro. Puede generarse localmente con `python -c "import secrets; print(secrets.token_urlsafe(32))"`. No compartir ni subir `.env` a Git.

El ejemplo selecciona `CAPRI_ENV=dev`, `CAPRI_ENGINE=fake` y `CAPRI_MEMORY=local`. Solo `CAPRI_API_KEY` es obligatoria. No se realizan llamadas de terceros en esta configuración.

```powershell
.\.venv\Scripts\python.exe -m uvicorn capri_core.main:create_app --factory --host 127.0.0.1 --port 8000
```

Ejecutar desde la raíz del repositorio. Para Tasker en otro dispositivo, usar un despliegue HTTPS o una red privada de confianza; no publicar el puerto en Internet sin TLS. No se instala ni configura infraestructura de red en esta fase. Usar **un solo worker**: el temporizador y la coordinación de psique residen en el proceso. El respaldo y el Watchdog solo funcionan mientras el backend permanece activo; no se ha instalado un servicio 24/7 ni una tarea externa.

## Estructura del SDD

```text
capri_core/
├── main.py
├── memory/
│   ├── mem0_client.py
│   └── backup_cron.py
├── psyche/
│   ├── core_directives.json
│   ├── psyche_state.json
│   └── prompt_builder.py
├── mcp_tools/
│   ├── system_actions.py
│   ├── recursive_edit.py
│   └── finance_manager.py
├── voice/
│   └── voice_engine.py
└── telemetry/
    ├── webhook_receiver.py
    └── fuzzy_logic.py
```

Los módulos auxiliares `config.py` y `cognitive.py`, las dependencias, esta documentación y `tests/` están fuera de `capri_core/`. Los paquetes son namespace packages de Python: no se agregan `__init__.py` al árbol del SDD.

## Configuración y proveedores

| Variable | Uso |
|---|---|
| `CAPRI_API_KEY` | Único secreto obligatorio; header `X-API-Key`. |
| `CAPRI_ENV` | `dev` o `prod`; fake no se permite en producción. |
| `CAPRI_ENGINE` | `fake`, `ollama`, `cloud` o `auto`. |
| `CAPRI_MEMORY` | `local`, `mem0` o `auto`. |
| `CAPRI_DATA_DIR` | Datos locales; por defecto `.data/`. |
| `GEMINI_API_KEY`, `GEMINI_API_URL`, `GEMINI_MODEL` | Adaptador real Gemini REST. Modelo/URL se configuran, no se codifican. |
| `OLLAMA_URL`, `OLLAMA_MODEL` | Adaptador real para una instancia existente; modelo del SDD: `gemma4:e4b`. |
| `MEM0_API_KEY`, `MEM0_API_URL`, `MEM0_USER_ID` | Mem0 Platform opcional; el ID delimita las memorias del propietario. |
| `CAPRI_BACKUP_KEY` | Clave Fernet opcional para el respaldo cifrado. |
| `CAPRI_BACKUP_HOUR` | Hora local diaria del respaldo; ejemplo: 03:00. |
| `CAPRI_TIMEZONE` | Zona de rutinas y respaldo; `America/Mexico_City`. |
| `CAPRI_LOW_HEART_RATE` | Límite exclusivo para frecuencia baja; ejemplo: 60 lpm. |
| `CAPRI_HTTP_TIMEOUT`, `CAPRI_TIMER_INTERVAL` | Timeout de proveedores y evaluación temporal, en segundos. |
| `ELEVENLABS_ENABLED`, `ELEVENLABS_API_KEY` | Reservados para la interfaz de voz; **siempre stub en Fase 1**. |

Las variables del proceso prevalecen sobre `.env`. `.env.example` contiene solo placeholders. No se guardan claves en el código, mensajes de Git o logs.

### Motor cognitivo

En `fake`, las respuestas son explícitamente simuladas y deterministas. En `ollama`, el backend usa `/api/chat` de la instancia configurada. En `cloud/auto`, usa Gemini si existe la llave; ante **403, 429, timeout o pérdida de conexión** reintenta la misma interacción en Ollama. Otros errores de nube no se ocultan como si fueran fallback. Si faltan credenciales de nube se selecciona el motor local; si Ollama no está configurado o no responde, `/api/chat` devuelve **503 controlado**, mientras finanzas y telemetría siguen operando.

No se descarga `gemma4:e4b` ni se comprueba su disponibilidad mediante red durante las pruebas. Configurar y comprobar el modelo en la laptop corresponde al usuario. Los nombres de modelos de terceros pueden cambiar: el ejemplo de Gemini debe ajustarse a los modelos disponibles en la cuenta al obtener una llave.

Las herramientas disponibles son `overwrite_psyche_trait`, `query_recent_expenses` y la categorización interna descrita en la sección 6 del SDD. Gemini usa function calling REST. Ollama usa un protocolo JSON validado de llamadas para no exigir que Gemma soporte herramientas nativas. Se limitan las iteraciones y se rechazan herramientas o argumentos no permitidos; nunca se ejecuta shell arbitrario desde una respuesta del modelo.

### Memoria y respaldo

La memoria local usa SQLite con la misma interfaz asíncrona que Mem0: inicialización, inserción/resolución de conflictos, búsqueda, exportación completa y cierre. Una clave de conflicto identifica hechos que deben sustituirse, conservando información de revisión; no se requieren embeddings, `bge-small` ni un proveedor externo en desarrollo. El adaptador Mem0 real está separado del almacenamiento local.

El respaldo exporta la memoria como JSON **dentro de un archivo cifrado**, nunca como un JSON legible. Para activar el respaldo, generar una clave local con:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Guardar el resultado solo en `CAPRI_BACKUP_KEY` de `.env`, y conservar una copia segura fuera del repositorio: sin ella no puede descifrarse el respaldo. **Sin clave válida, el respaldo está deshabilitado con una advertencia, pero el servidor arranca.** Los respaldos quedan en el directorio local de datos. Esta fase no copia archivos a otros equipos ni configura Supabase.

### Psique y Git

`core_directives.json` contiene el W++ literal del SDD y no es un destino permitido de ninguna herramienta. `psyche_state.json` contiene `mood`, `trust`, `sarcasm_level` y `vad` (`valence`, `arousal`, `dominance`). La allowlist y los rangos se validan antes de escribir. Los números se reciben como cadenas en la firma del SDD y se validan contra el esquema real del estado.

Cada edición exige un commit previo del estado válido, incluso si es un commit vacío, con un índice Git aislado para no incluir archivos staged ajenos ni omitir hooks. La auditoría registra la intención antes de escribir; si Git o la auditoría fallan, el estado no se modifica. Se rechazan enlaces simbólicos y hardlinks como destinos. El Watchdog verifica al arrancar y vigila corrupción durante la ejecución; recupera **solo `psyche_state.json`** desde el último commit que contiene un estado válido, sin resetear el repositorio entero. No recupera un estado si no existe una revisión válida: falla de forma cerrada. Los commits automáticos de psique son locales; no generan pushes autónomos.

El repositorio debe tener identidad Git ya configurada y una revisión válida inicial. No se cambia la configuración global del usuario. Una copia descomprimida sin `.git` sirve para finanzas/telemetría, pero **no puede usar edición autónoma con commit** hasta inicializar Git y registrar el estado.

## Endpoints y ejemplos curl

Existen únicamente estas tres rutas POST. **Todas** requieren `X-API-Key`; se comprueba antes de validar el payload. Ausencia, clave incorrecta o headers duplicados: **401**. Swagger, ReDoc y OpenAPI están deshabilitados. Con clave válida, rutas inexistentes devuelven 404.

En una terminal Bash, establecer `CAPRI_API_KEY` con el mismo secreto de `.env`, sin pegarlo en scripts versionados. En PowerShell usar `curl.exe` en lugar del alias `curl`, `$env:CAPRI_API_KEY` en el header y, para evitar problemas de quoting, un archivo JSON temporal privado con `--data-binary @archivo.json`.

### Conversación: `POST /api/chat`

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "X-API-Key: $CAPRI_API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"Hola, Capri"}'
```

Devuelve `reply`, `provider`, `fallback`, resultado de voz stub y resultados de herramientas. Con memoria vacía se inyecta el sentinel de inicialización de la sección 7; el fake reproduce la presentación del ejemplo. Las conversaciones no financieras se guardan en la memoria seleccionada. Si hay gastos recientes sin categoría, se agrega una pregunta proactiva local después de guardar la parte no financiera: ese recordatorio no se persiste en Mem0 ni se reenvía a Gemini.

### Finanzas: `POST /api/finance/add_expense`

```bash
curl -X POST http://127.0.0.1:8000/api/finance/add_expense \
  -H "X-API-Key: $CAPRI_API_KEY" -H "Content-Type: application/json" \
  -d '{"amount":"800.01","concept":"Hardware"}'
```

Respuesta 201 con `amount` exacto, categoría `null` y `needs_category: true`. El monto se almacena como centavos enteros; enviar decimales como cadenas, con máximo dos decimales. Se aceptan enteros, pero se rechazan floats JSON para no perder precisión antes de la validación. Concepto obligatorio (1–240 caracteres), categoría opcional (1–80); montos no positivos, no finitos o con precisión extra se rechazan con 422.

La consulta reciente es una herramienta interna, **no una ruta GET adicional**. Para identificar un gasto:

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "X-API-Key: $CAPRI_API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"Revisa mis gastos"}'
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "X-API-Key: $CAPRI_API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"categoría: equipo"}'
```

El flujo financiero se responde localmente: ni los registros de SQLite ni los resultados de herramientas financieras se envían a Gemini o Mem0 ni aparecen en logs. La categorización exige una respuesta explícita para el gasto pendiente. El estado de esa conversación es del proceso; si se reinicia, consultar otra vez. Al usar un motor de nube, evitar introducir datos financieros o secretos en conversaciones generales: la protección del flujo de finanzas no es un detector universal de información sensible en texto libre.

### Tasker: `POST /api/telemetry/tasker`

```bash
curl -X POST http://127.0.0.1:8000/api/telemetry/tasker \
  -H "X-API-Key: $CAPRI_API_KEY" -H "Content-Type: application/json" \
  -d '{"location":"ipn","latitude":19.504,"longitude":-99.146,"battery_percent":85,"charging":false,"heart_rate":75,"activity":true}'
```

`location` admite `other`, `ipn` o `sports`. Las coordenadas son opcionales y se validan; **no se inventaron geocercas**. El payload es un snapshot: los campos omitidos toman sus defaults, por lo que Tasker debe enviar juntos los valores de ubicación, carga y pulso que quiera mantener. `activity: true` indica actividad real y reinicia la inactividad; un heartbeat sin actividad no la reinicia. No se configura Tasker ni se recolectan datos del teléfono en esta entrega.

Prueba de intención de cansancio (sin leer ventanas ni generar audio):

```bash
curl -X POST http://127.0.0.1:8000/api/telemetry/tasker \
  -H "X-API-Key: $CAPRI_API_KEY" -H "Content-Type: application/json" \
  -d '{"active_app":"Code::Blocks","active_app_hours":3.1}'
```

La respuesta contiene estado temporal, probabilidad de sueño, eventos y entregas simuladas a los stubs.

## Reglas temporales aprobadas

La inactividad se acumula con un reloj inyectable para las pruebas. Primera alerta a **2 h** normalmente, o **4 h** en IPN/deporte. Preocupación activa a **5 h**, intención crítica a **12 h o más**. Cada fase se emite una vez por ciclo de actividad, no en cada heartbeat. El temporizador evalúa sin necesitar una nueva petición.

Entre **00:00 y 06:00** (06:00 excluido), cargando y con frecuencia cardíaca **menor que 60 lpm** configurable, probabilidad de sueño **0.95**: se pausa el contador sin borrar el tiempo acumulado. Sin frecuencia cardíaca o con actividad explícita no se infiere sueño. Una muestra caduca a las seis horas; para mantener la inferencia, Tasker debe actualizarla. Se integran exactamente los intervalos contra medianoche, las 06:00 y la caducidad, aunque no hubiera una evaluación en el límite: no se conserva una pausa nocturna indefinidamente ni se reinterpretan intervalos anteriores con una muestra nueva.

Code::Blocks o un PDF activo por **más de 3 h**, no exactamente 3 h, produce una vez por sesión:

```text
<voice_note>Deja de compilar, tu cerebro ya está frito. Cierra eso.</voice_note>
```

Audio forzoso, `<voice_note>`, `<humming>` y notificación crítica son **intenciones**; los stubs no sintetizan voz, controlan ventanas ni ejecutan acciones en el sistema, incluso con ElevenLabs habilitado. La inferencia de sueño es una heurística del SDD, no una evaluación médica.

## Pruebas offline

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

La suite bloquea transportes HTTP reales y usa relojes, proveedores y repositorios temporales. Cubre API Key en todas las rutas, payloads, límites y pausas temporales, finanzas exactas y categorías NULL, fallback 403/429/transporte, función de edición y recuperación, W++ literal, memoria local/HTTP mock y cifrado. No se utiliza `.env` real ni se requieren llaves de terceros. Los tests de Git configuran identidad solo en repositorios temporales.

Para comprobar el rechazo de una petición sin API Key:

```bash
curl -i -X POST http://127.0.0.1:8000/api/finance/add_expense \
  -H "Content-Type: application/json" -d '{"amount":"1.00","concept":"Prueba"}'
```

Debe devolver 401. No hay un endpoint sin protección para comprobar salud.

## Pendiente fuera de Fase 1

El usuario deberá probar conectividad real con Ollama y, cuando tenga llaves, con Gemini y Mem0. Los tests verifican contratos y errores mediante mocks, no disponibilidad de servicios o modelos remotos. ElevenLabs, audio real, notificaciones nativas, control de SO/Spotify, overlay DWM/X11 y `bge-small` permanecen fuera de esta entrega, con interfaces/TODO de Fase 2 donde corresponde.
