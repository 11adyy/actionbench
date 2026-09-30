# Plan de reparación y validación de ActionBench

## 0. Encargo para la IA que implemente este plan

Repara el harness existente hasta que permita evaluar de verdad la hipótesis de ActionBench. Una action es un ejecutable reutilizable asociado a una skill, con acceso al modelo a través de un broker controlado. El estudio compara esta organización con agentes sin skill, con skill, con scripts deterministas y con código con llamadas al modelo generado durante la ejecución.

El objetivo no es hacer que las actions ganen. Debes poder obtener un resultado favorable, desfavorable o inconcluso con un experimento que funcione y permita distinguir esas posibilidades. Un resultado negativo válido se conserva; un error del harness se diagnostica y no se vende como evidencia científica.

Trabaja sobre el repositorio existente. Implementa en commits coherentes, conserva los registros originales y demuestra cada reparación mediante las pruebas indicadas. No sustituyas el sistema por una demo. No declares terminado el trabajo porque pasen tests con clientes simulados.

Este documento es un plan: al redactarlo no se han aplicado las reparaciones descritas ni iniciado otra campaña.

Repositorio: https://github.com/11adyy/actionbench

Directorio local: `/Users/noahpelegrini/Documents/Codex/2026-09-27/s/outputs/actionbench`

Base inspeccionada: commit `9d6e2de`.

Evidencia principal: [ejecución pilot-002](https://github.com/11adyy/actionbench/actions/runs/36652223989), artefacto `11071731946`.

Copia local de auditoría, fuera del repositorio: `../actionbench-audit/pilot-002/`. Contiene el ZIP original, la base SQLite, la configuración y los informes. No contiene el valor de la API key.

## 1. Qué está confirmado y qué no

### 1.1 Resultado observado

| Indicador | Resultado de pilot-002 |
|---|---:|
| Evaluaciones de test planificadas | 600 |
| Evaluaciones de test terminadas con puntuación | 14 |
| Evaluaciones de test registradas como fallo | 586 |
| Paquetes action creados | 0 de 6 |
| Paquetes skill_script creados | 0 de 6 |
| Paquetes skill creados | 6 de 6 |
| Invocaciones de herramientas en test registradas en procedure_usage | 0 |
| Solicitudes al proveedor registradas con respuesta y uso | 504 |
| Respuestas del proveedor con estado incomplete | 2 |
| Coste contabilizado por el harness | 0,14418341 USD |
| Límite configurado | 8 USD |

Los 600 episodios corresponden a 2 familias × 20 tareas × 3 réplicas × 5 condiciones. No son 600 tareas independientes. El resumen general de SQLite muestra 23 episodios completados y 692 fallidos porque incluye creación, desarrollo e integración; no confundir ese denominador con test.

GitHub terminó en verde, la campaña quedó frozen y existe un marcador complete. Sin embargo, eso solo demuestra que el programa llegó a un estado terminal. No demuestra que haya evaluado satisfactoriamente la arquitectura propuesta. El coste indicado es contabilidad del harness, no una conciliación con la factura del proveedor.

### 1.2 Causas confirmadas por código y respuestas guardadas

1. **Identificadores incompatibles con el contrato del generador.** `_validate_procedure` usa `procedure_id.replace('-', '').isalnum()`: rechaza `_`. Las respuestas contienen identificadores razonables como `extract_function_contract`. Al volver a ejecutar las validaciones sobre las 30 respuestas de creación de action/skill_script guardadas, 18 se rechazan inicialmente por este motivo: 7 action y 11 skill_script. Que pasen esa validación después de corregirla no demuestra todavía que sus programas funcionen.
2. **Protocolo de salida ambiguo.** `AgentRunner` exige un objeto con `type`, mientras el contenido de la tarea exige directamente código Python o el objeto de respuesta de HotpotQA. El registro contiene código y respuestas del benchmark que el harness descarta por no incluir el envoltorio del agente. Es un conflicto de contratos confirmado; no se ha demostrado que sea la única causa del comportamiento del modelo.
3. **JSON exigido solo mediante instrucciones.** El broker no envía un esquema estructurado para decisiones y paquetes. Hay respuestas de creación con texto adicional, varios objetos o contenido truncado.
4. **Respuestas incompletas no diferenciadas.** Dos respuestas tienen `status=incomplete`, motivo `max_output_tokens`. La llamada puede estar correctamente contabilizada y, a la vez, no proporcionar una salida utilizable. Actualmente no se expresa bien esa diferencia.
5. **Errores de creación poco visibles.** Los detalles intermedios se reutilizan como feedback, pero el episodio acaba con el mensaje genérico «No valid ... package was produced». El informe no explica qué validación falló en cada revisión.
6. **Conclusiones de coste engañosas.** El informe activa `observed_cost_saving_at_nonnegative_quality` en algunas comparaciones donde no se creó ni ejecutó ninguna action. También permite intervalos degenerados y cálculos de amortización en esos casos.
7. **Estados publicados inconsistentes.** `status.json` y `report.json` se escriben antes de freeze, por lo que muestran draft aunque la base de datos ya está frozen.
8. **El worker puede ocultar errores.** Captura el código de salida de la ejecución sin devolverlo; además, `report ... || true` permite continuar aunque falle el informe.

No atribuir todo a que Luna sea poco capaz ni cambiar de modelo como primera reparación. Tampoco considerar erróneo todo fallo del agente: una vez validado el protocolo, un agente puede fallar legítimamente y ese resultado debe contarse según el análisis predefinido.

## 2. Reglas que deben conservarse

- Mantener las cinco condiciones: `plain`, `skill`, `skill_script`, `improvised`, `action`.
- Mantener las referencias de evaluación fuera del contenedor del agente y del contexto del creador. Solo desarrollo puede alimentar la selección de paquetes.
- Mantener Docker, límites de recursos, ausencia de red en ejecutables y broker como único acceso autorizado al modelo.
- Mantener la contabilidad de todas las llamadas: creación, reparación, desarrollo, ejecución e integración, desglosadas por fase.
- Mantener el bloqueo de solicitudes cuyo resultado se desconoce; no reintentarlas ciegamente.
- Mantener huellas de configuración, código, manifiesto, paquetes e imágenes. Cambiar el harness exige otra campaña, no reinterpretar una campaña congelada como si hubiera usado el código nuevo.
- Conservar la aleatorización determinista del orden de evaluación que ya existe en `_execute`.
- No introducir paquetes escritos a mano como sustitutos de los paquetes generados del experimento. Se permiten controles conocidos exclusivamente en pruebas de infraestructura identificadas como tales.
- No aumentar presupuesto ni cambiar modelo o esfuerzo de razonamiento silenciosamente. Registrar el gasto acumulado de los intentos anteriores; no tratar cada nueva campaña como autorización de otro presupuesto ilimitado.

## 3. Fase 1: preservar evidencia y reproducir los fallos

**Archivos:** nuevo script de auditoría offline, fixtures de regresión y documentación.

1. Calcular y guardar SHA-256 del artefacto original y de su configuración, base e informes.
2. Abrir SQLite en modo de solo lectura durante auditorías. No modificar pilot-002 ni borrar episodios fallidos.
3. Crear un auditor que derive de SQLite los recuentos de la sección 1, las causas de fallo por fase y las respuestas incompletas. No escribir esos números a mano como supuestos del programa.
4. Extraer fixtures mínimos de respuestas reales: un ID con `_`, salida de benchmark sin envoltorio, creación con texto adicional, creación con más de un objeto y respuesta truncada.
5. En cada fixture registrar origen y qué comportamiento comprueba. Evitar publicar el dataset privado o trazas extensas innecesarias; los casos sintéticos pueden cubrir las estructuras sin copiar preguntas completas.
6. Reproducir el falso indicador de ahorro con un fixture pequeño: todos los paquetes action ausentes, puntuación cero y coste de ejecución cero.

**Aceptación:** el auditor reproduce 600/14/586 y 0/6 paquetes action sin tocar la evidencia. Las pruebas nuevas fallan por las causas conocidas antes de aplicar las correcciones. Los fixtures offline se etiquetan como regresión, no como evaluación real nueva.

## 4. Fase 2: definir un protocolo común y sin contradicciones

**Archivos:** `actionbench/agent.py`, `actionbench/skill_creator.py`, `actionbench/broker.py`; crear un módulo pequeño de contratos compartidos si evita duplicaciones.

### 4.1 Decisiones del agente

Separar explícitamente:

- El **protocolo del agente**, que selecciona herramienta o finalización.
- El **contenido de la respuesta final**, cuyo formato pertenece al benchmark.

Normalizar `task_input` a un objeto cuando corresponda, en vez de incluir una cadena JSON dentro de otra cadena JSON. No cambiar preguntas, datos, respuestas de referencia ni contratos de los graders.

Usar salida estructurada en la API para las decisiones. Ruta recomendada: Responses con `text.format`, tipo `json_schema` y `strict: true`. Antes de implementarla verificar el contrato actual y probarlo con el modelo y endpoint configurados. Referencia: [Structured outputs de OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs).

Propuesta simple de envoltorio estable:

```json
{
  "type": "final",
  "answer": "contenido final del benchmark",
  "code": null,
  "procedure_id": null,
  "input_json": null
}
```

Todos los campos deben estar declarados; los no aplicables admiten null. El enum de `type` solo incluye operaciones habilitadas para la condición. Validar localmente las combinaciones: final exige answer; code exige código; procedure exige un ID del catálogo y entrada válida.

`input_json` permite transportar entradas dinámicas sin hacer depender el esquema externo de objetos arbitrarios incompatibles con el subconjunto de JSON Schema del proveedor. Decodificarlo exactamente una vez y validarlo contra `input_schema` antes de ejecutar. Otra representación es aceptable si tiene una prueba real de compatibilidad y conserva el mismo contrato funcional.

Para MBPP+, `answer` contiene el código que recibe el grader. Para HotpotQA, `answer` contiene la serialización del objeto de respuesta que espera su grader. Dar ejemplos cortos de ambos formatos sin usar respuestas del conjunto de test.

Usar el mismo mecanismo y las mismas reglas de recuperación en las cinco condiciones. No facilitar un formato a action y otro más frágil a las baselines.

### 4.2 Recuperación de errores del agente

Añadir feedback claro para decisiones semánticamente inválidas y errores recuperables de herramientas. Regla inicial: máximo 2 intentos de reparación de protocolo por episodio, incluidos dentro del límite global de llamadas y tokens, sin presupuesto extra oculto.

Cada intento debe tener identidad, coste, respuesta y motivo registrados. Al agotarse el límite, cerrar con un fallo explícito de protocolo. Los fallos de infraestructura y las solicitudes de resultado desconocido deben propagarse y detener/pausar, no disfrazarse de observaciones corregibles por el agente.

No aceptar silenciosamente código suelto como respuesta final, extraer «el primer JSON que parezca válido» ni ignorar texto posterior. Eso ocultaría el problema y haría ambiguo qué se ejecutó.

**Aceptación:** fixtures válidos de final, code, llm_code y procedure llegan al consumidor correcto; operaciones prohibidas se rechazan; una respuesta final de HotpotQA no se confunde con una decisión; un error recuperable puede corregirse con coste registrado; una pérdida de conexión no genera un retry automático.

## 5. Fase 3: corregir la creación de paquetes

**Archivos:** `actionbench/skill_creator.py`, `_create_packages` en `commands.py`, ledger en `store.py`.

1. Definir una regla única de IDs, por ejemplo `^[A-Za-z][A-Za-z0-9_-]{0,63}$`. Compartirla entre prompt, esquema y validación. Rechazar rutas, `..`, `/`, barras invertidas, IDs vacíos y duplicados. No convertir nombres silenciosamente porque puede romper referencias.
2. Para skill, pedir solo `skill_md`. Para action y skill_script, pedir solo las procedures. El harness debe copiar la skill emparejada congelada al paquete: no pedir al modelo que la retranscriba byte por byte. Verificar después su hash para conservar el emparejamiento.
3. Fijar `command` en el harness a la ejecución permitida. El modelo no necesita generar ese dato invariable.
4. Usar también salida estructurada para el manifiesto de creación. Si el esquema de entrada de cada procedure es dinámico, transportarlo como `input_schema_json`, decodificarlo y validarlo con `Draft202012Validator`.
5. Validar ID, unicidad, código Python, esquema de entrada, protocolo y políticas de acceso al modelo antes de publicar un paquete. Mantener escritura provisional y publicación atómica.
6. Guardar por revisión: request ID, respuesta utilizable o motivo de rechazo, tipo de error, mensajes de validación, coste, huella y resultados de desarrollo. No quedarse solo con el mensaje genérico del episodio padre.
7. Hacer explícito el máximo de revisiones y el presupuesto agregado de creación. Actualmente pedir 4096 tokens otra vez puede exceder lo que queda de los 8000 del episodio. Ajustar el máximo solicitado al remanente o cerrar con `creation_budget_exhausted`; no presentarlo como JSON inválido.
8. Evitar paquetes desmesurados: empezar con 1–2 procedures cortas por paquete. Cualquier cambio de límites debe aplicarse simétricamente a action y skill_script y quedar congelado antes de la campaña.
9. Mantener selección por desarrollo. Diferenciar un paquete ejecutable con puntuación baja de un paquete que nunca pudo ejecutarse; no seleccionar este último como si hubiera pasado la validación técnica.
10. No forzar una victoria de action pidiendo a las baselines procedimientos inútiles. Sus instrucciones deben describir el mismo propósito y las capacidades disponibles.

**Aceptación:** los IDs reales con `_` superan la validación de nombre; los IDs peligrosos no. Una campaña pequeña con API real genera al menos un paquete operativo de cada tipo en cada familia, ejecuta su desarrollo y conserva la skill emparejada idéntica. Esta exigencia es un control previo de funcionamiento; en el experimento posterior se conservan también los fallos genuinos de creación.

## 6. Fase 4: estado del proveedor, presupuesto y trazabilidad

**Archivos:** `broker.py`, `store.py`, `errors.py`, configuración.

Separar el estado financiero de una llamada de la utilidad de su respuesta. Una respuesta incomplete con usage puede ser una llamada cobrada y conocida, pero un resultado inválido para el consumidor. No confundirla con una solicitud de resultado desconocido ni tratarla como JSON completo.

Registrar al menos estado del proveedor, motivo de incomplete, refusal cuando exista, request ID, modelo devuelto, tokens de entrada/salida/caché, coste y estado de validación del resultado. Conservar la respuesta original como evidencia.

Si se permite repetir una salida truncada, debe ser un intento nuevo, con clave propia y presupuesto restante; jamás borrar su primer coste. Ante respuesta incompleta conocida no hace falta reconciliación manual de una ejecución desconocida. Ante timeout ambiguo sí se mantiene ese bloqueo.

Incluir esquema estructurado, versión del contrato y opciones de razonamiento en el hash del payload y en la configuración congelada. Una llamada anterior no puede reutilizarse después de cambiar su esquema.

No aplicar el esquema del coordinador a todas las llamadas internas de las actions: algunas necesitan texto libre. El broker debe aceptar un contrato opcional por llamada; las decisiones y la creación sí deben especificarlo.

Mantener la contabilidad diferenciada de caché ya añadida y las reservas conservadoras. Mostrar por separado gasto confirmado y cantidades reservadas o inciertas. Verificar que cualquier nueva reparación de formato aparece en la misma contabilidad.

**Aceptación:** una llamada truncada queda cobrada exactamente una vez, no produce paquete; al reanudar, una llamada completada no se repite; cambiar el esquema impide la reutilización accidental; una solicitud ambigua queda bloqueada y visible.

## 7. Fase 5: separar finalización, validez y resultado científico

**Archivos:** `report.py`, `commands.py`, `store.py`, `design.py`.

No resolver esto exigiendo que las actions ganen para permitir freeze. Congelar significa conservar resultados inmutables, incluso malos. El error actual es confundir estados terminales con evidencia apta para la comparación.

Publicar tres dimensiones separadas:

1. `execution_status`: pending, running, paused, blocked o terminal.
2. `validation_status`: passed, failed o inconclusive, acompañado de razones y pruebas técnicas.
3. `scientific_status`: diagnostic_only, exploratory o confirmatory; ninguna equivale automáticamente a «hipótesis confirmada».

Mantener el estado frozen como propiedad de inmutabilidad independiente. Una campaña diagnóstica fallida puede archivarse congelada sin aparecer como estudio exitoso.

Añadir categorías de resultado que permitan distinguir, al menos: respuesta puntuable, error de protocolo, programa generado fallido, paquete no disponible, presupuesto de episodio agotado, fallo del harness, fallo de infraestructura, proveedor rechazado, resultado del proveedor desconocido y falta de trabajo por presupuesto de campaña.

Las categorías deben asignarse donde se conoce la causa; no reconstruirlas únicamente buscando frases en strings. Los registros históricos pueden auditarse con reglas explícitas, conservando los valores originales.

### 7.1 Cómo tratar los fallos en las métricas

- Los fallos genuinos del método, bajo un harness validado, pueden puntuar cero en el análisis por asignación original. Esto evita eliminar selectivamente fracasos.
- Los fallos de infraestructura o defectos conocidos del harness no son evidencia de incapacidad del método. Se informa cuántos hay y qué comparaciones quedan comprometidas.
- Un análisis solo entre episodios exitosos es secundario y condicionado; nunca reemplaza al principal sin advertir el sesgo de selección.
- Si no se ha creado/ejecutado ninguna action, la comparación de su rendimiento en uso no está identificada. Se puede informar del fracaso de creación, pero no afirmar ventajas de ejecución o amortización.
- No impedir un resultado científico negativo legítimo cuando el sistema funciona y el método falla por sus propios límites.

### 7.2 Corregir el informe

1. Añadir resumen de validez y causas antes de los promedios.
2. Mostrar paquetes intentados, validados estructuralmente, ejecutables, seleccionados y realmente invocados.
3. Mostrar puntuaciones, cobertura, fallos por categoría, reparaciones de protocolo y uso real de herramientas por familia/condición.
4. Separar coste de creación, desarrollo, ejecución y controles; mostrar coste total para un horizonte de reutilización declarado.
5. No llamar ahorro a un delta de coste igual a cero. No activar un indicador de mejora por coste cero derivado de no ejecutar un paquete.
6. Para sostener ahorro sin pérdida de calidad, predefinir margen de no inferioridad, horizonte de amortización y criterio inferencial. Una diferencia media de calidad no negativa, por sí sola, no basta.
7. Si la comparación no es interpretable, devolver `null` y razones para conclusiones y amortización. Conservar datos descriptivos y fallos; no esconderlos.
8. Mantener bootstrap pareado por tarea y réplica de paquete. No tratar las 60 celdas por condición como 60 tareas independientes. Tres réplicas siguen ofreciendo poca información sobre variabilidad entre paquetes.
9. `plan_sample` debe rechazar campañas diagnostic_only o contrastes no identificados. Una cuadrícula de ceros por fallos técnicos no puede producir una planificación de potencia aparentemente perfecta.
10. Corregir o renombrar `primary_with_failures_as_zero`: hoy usa episodios terminales como denominador. En campañas parciales declarar ese denominador; no convertir pendientes en resultados observados.

**Aceptación:** el replay offline de pilot-002 genera un resumen diagnóstico sin afirmación de ahorro o amortización de actions. Un control con actions ejecutadas y peores puntuaciones conserva su resultado negativo. Un control con puntuaciones válidas y menor coste sí permite el análisis previsto, sin convertirlo automáticamente en evidencia confirmatoria.

## 8. Fase 6: GitHub Actions debe publicar un estado fiel

**Archivos:** `cloud/github_worker.sh`, `cloud/github_state.sh`, `.github/workflows/evaluate.yml`, documentación.

1. Preservar el código de salida de cada fase. Fallos de infraestructura, validación técnica o generación del informe deben verse como tales en GitHub.
2. Quitar el silenciamiento indiscriminado del informe. Si falla, guardar el error y marcar fallida esa fase.
3. Mantener archivo/subida con `always()` aunque una fase falle. Evitar perder la base por propagar un error antes de empaquetar.
4. Escribir `status.json` y el informe final después de la transición de estado; comprobar que coinciden con SQLite y el marcador final.
5. Sustituir la interpretación de un archivo complete vacío por metadatos verificables: campaña, huellas, finalización y validación. No reanudar una campaña solo por encontrar un marcador sin verificar.
6. Una salida por tiempo o presupuesto debe decir pausada/incompleta, conservar lo pendiente y explicar cómo continuar. Una petición de revisión por resultado desconocido debe aparecer como bloqueada.
7. Si todos los fallos son resultados legítimos del método bajo un harness validado, el workflow puede terminar correctamente y el resumen debe decirlo. Un resultado negativo no equivale a fallo de infraestructura.
8. Añadir resumen de job con campaña, modelo, coste, episodios puntuados, fallos por causa, validez y enlaces a artefactos.
9. Revisar recuperación de artefactos: el listado actual solo consulta los primeros 100. Paginar y elegir por campaña exacta leyendo metadatos; un prefijo de nombre puede confundir campañas como `pilot` y `pilot-extra`.
10. Probar los cambios en una campaña nueva. Mantener el bloqueo de cambios de código/imagen/configuración durante resume. No actualizar en secreto el source SHA de pilot-002.

**Aceptación:** provocar deliberadamente un fallo técnico produce job fallido con artefacto descargable; pausar conserva el estado; un resultado negativo válido sigue siendo distinguible; informe, SQLite y resumen de GitHub coinciden.

## 9. Fase 7: pruebas reales por capas y puertas de avance

No lanzar otras 600 evaluaciones inmediatamente. Ejecutar en este orden y guardar evidencia de cada puerta. Los nombres de comandos nuevos que se creen deberán documentarse; no fingir que existen hoy.

### A. Regresión offline

Ejecutar las pruebas existentes y las de los fallos observados. Se permiten respuestas guardadas o simuladas para contabilidad, validadores y estados. Etiquetar este nivel como offline. No demuestra que el modelo respete el esquema ni que Docker funcione.

### B. Docker real, sin modelo

Ejecutar un programa determinista mediante el runner de producción, no mediante `LocalProcessRunner`. Probar entradas y salidas JSONL, una ruta con `:`, montaje de action de solo lectura, workspace escribible, timeout y limpieza del contenedor. Conservar controles correcto/incorrecto de los graders oficiales de ambas familias.

No quitar restricciones para que pase la prueba. La prueba local que sustituye Docker por Python sigue siendo útil, pero no reemplaza esta puerta.

### C. API real y action de control

Con la clave que ya está en GitHub:

1. Probar una decisión final estructurada y su serialización hacia cada grader.
2. Ejecutar dentro de Docker una action de control que haga una llamada real a través del broker y devuelva su resultado. Ese control no entra en las métricas de rendimiento del estudio.
3. Comprobar en la traza la cadena contenedor → broker → request del proveedor → respuesta → resultado de action.
4. Probar que skill_script no tiene acceso al modelo.
5. Verificar que modelo y endpoint aceptan el esquema y las opciones configuradas. Si no, detener y documentar; no sustituirlos silenciosamente.

### D. Canary generado por el modelo

Usar solo tareas de desarrollo y una réplica por familia. Generar realmente skill, skill_script y action, sin proporcionar código resuelto. Ejecutar las cinco condiciones sobre un subconjunto pequeño fijado antes de ver sus resultados.

Registrar generación, revisiones, validación, decisiones, llamadas a herramientas, llamadas internas y evaluación independiente. No exigir respuestas correctas en todos los casos: exigir que la ruta completa se ejecute y los resultados sean puntuables o tengan un fallo del método explícito.

La ruta de la action debe probarse con una invocación directa de integración usando una entrada compatible con su esquema; eso no es el resultado del agente autónomo. En la evaluación natural, permitir que el agente decida si usa la action. Si nunca la usa, informar de falta de adopción y no confundirlo con mejora de su ejecución. Una evaluación con uso forzado, si se añade, debe ser una ablación separada.

**Puerta para pasar al piloto:** las tres clases de paquete tienen al menos un ejemplar ejecutable por familia; la ruta generada con llamada interna al modelo está demostrada; no hay defectos técnicos pendientes que invaliden todas las condiciones. Los errores aislados del método se conservan, no se borran hasta conseguir una corrida bonita.

### E. Reanudación real entre runners

Ejecutar una campaña corta, guardar estado, terminar el primer runner y continuar en otro. Confirmar que se recuperan configuración, imágenes exactas, paquetes y ledger; los episodios ya evaluados no se vuelven a cobrar.

Cubrir además puntos de interrupción controlados: antes de enviar una solicitud, después de guardar su respuesta, después de guardar respuesta final y antes del grader, y después de guardar la evaluación. Para la ventana ambigua después del envío pero antes de persistir respuesta, usar fault injection y verificar bloqueo; no afirmar exactamente-una-vez frente a una API remota sin pruebas de reconciliación.

Comparar los resultados ya confirmados antes/después de la reanudación, hashes y número de solicitudes nuevas. No exigir que dos campañas estocásticas independientes produzcan respuestas byte-idénticas.

### F. Nuevo piloto completo

Solo después de A–E: nueva campaña con configuración, código, manifiesto, contratos y precios congelados. Mantener el modelo elegido inicialmente para aislar el efecto de reparar el harness. Si se propone cambiar de modelo o reasoning, tratarlo como otra configuración experimental.

Los datos usados para depurar pilot-002 ya están expuestos. Ese conjunto puede servir para regresión o un piloto exploratorio, pero no presentarlo después como confirmación independiente. Reservar tareas nuevas para el estudio confirmatorio y registrar la separación.

## 10. Matriz mínima de pruebas de aceptación

| Prueba | Evidencia exigida |
|---|---|
| ID con guion bajo | Aceptado; ID y referencias conservados |
| ID con traversal o duplicado | Rechazado antes de escribir/ejecutar |
| JSON con texto adicional o varios objetos | Rechazo tipado, sin extracción heurística |
| Respuesta truncada | Uso cobrado, motivo registrado, sin paquete parcial |
| Final MBPP+ | Envoltorio retirado correctamente; grader recibe código |
| Final HotpotQA | Grader recibe el objeto esperado, no la decisión externa |
| Herramienta no habilitada | Rechazada, con reparación limitada si aplica |
| Entrada de procedure inválida | Validación de schema antes de ejecutar |
| Recuperación de protocolo | Máximo de intentos respetado; costes incluidos |
| Skill emparejada | Hash idéntico en skill/action/skill_script |
| Solicitud conocida al reanudar | Sin llamada ni cobro nuevos |
| Solicitud ambigua | Bloqueo y explicación para reconciliar |
| Cambio de schema/config/imagen | Reutilización o resume rechazados |
| Action sin paquete | Fallo de creación visible; sin falso ahorro |
| Action válida pero peor | Resultado negativo preservado |
| Todas las condiciones con fallo técnico | Diagnóstico; no conclusión de eficacia |
| Presupuesto de campaña agotado | Pendientes conservados, no convertidos en ceros |
| Fallo del informe | Job y resumen reflejan error; ledger archivado |
| Ruta Docker con `:` | Programa ejecutado en Docker real |
| Graders correctos/incorrectos | Puntuaciones esperadas en ambos benchmarks |
| Action generada y llamada real | Request ID, uso y salida enlazados |
| Reanudación en otro runner | Artefactos verificados, sin repetir trabajo confirmado |

No basta con una lista de checks marcados. Para cada prueba real entregar campaña, commit, job, comando ejecutado, resultado y artefacto verificable.

## 11. Orden sugerido de commits

1. Auditoría reproducible, fixtures mínimos y diagnóstico de pilot-002.
2. Contratos compartidos, IDs, generación de paquetes y preservación de skill emparejada.
3. Salidas estructuradas, estado del proveedor, recuperación limitada y contabilidad.
4. Categorías de fallo, validez de comparación, informe y planificación de muestra.
5. Estado del workflow, preservación de errores, artefactos y recuperación.
6. Pruebas Docker/API/canary/reanudación, documentación y evidencia de las ejecuciones reales.

Cada commit debe ser coherente y pasar sus pruebas pertinentes. Ajustar los límites entre commits si hay dependencias; no hacer un commit por cada línea ni uno gigante con todo. No reescribir commits o resultados ya publicados para ocultar errores anteriores.

## 12. Criterio final de entrega

El trabajo está terminado cuando otra persona puede iniciar una campaña con su clave, verificar una action generada llamando al modelo bajo los límites del harness, obtener puntuaciones de los graders independientes, interrumpir y reanudar sin repetir solicitudes confirmadas, y leer un informe que distinga los resultados del método de los fallos técnicos.

Entregar:

- Commits y resumen de cambios.
- Diagnóstico reproducible del piloto fallido.
- Evidencia real de cada puerta A–E.
- Informe de la nueva campaña, o estado exacto y reanudable si aún está ejecutándose.
- Coste confirmado, reservas pendientes y gasto acumulado de los intentos.
- Problemas residuales y qué afirmaciones científicas permiten los datos.

No concluir «la hipótesis funciona» solo porque la infraestructura ya pasa las pruebas. Para un paper todavía habrá que fijar hipótesis, análisis principal, tamaño de muestra, tareas nuevas y alcance de generalización. Dos familias y un modelo pueden sustentar un estudio acotado; no justifican una afirmación universal sobre todas las skills o agentes.
