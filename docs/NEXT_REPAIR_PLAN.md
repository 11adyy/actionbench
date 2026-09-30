# ActionBench: plan de trabajo pendiente para una evaluación publicable

Este documento es un encargo ejecutable para la siguiente IA. Trabaja sobre el repositorio existente; **no hagas una demo ni reemplaces llamadas reales por mocks para declarar victoria**. La hipótesis es que una *action* —un ejecutable reutilizable asociado a una skill, capaz de llamar al modelo mediante el broker controlado— puede mejorar calidad, coste o ambos frente a las alternativas. El diseño debe admitir también un resultado negativo o inconcluso.

## 1. Estado comprobado al redactar este plan

- Repositorio: `https://github.com/11adyy/actionbench`. Directorio: `/Users/noahpelegrini/Documents/Codex/2026-09-27/s/outputs/actionbench`. Base inspeccionada: `ec56658`. Trabaja en commits de alcance normal y súbelos.
- `pilot-002` es **evidencia diagnóstica**, no evidencia a favor ni en contra de la hipótesis: de 600 episodios de test, solo 14 fueron puntuados, 586 fallaron; se creó 0/6 paquetes action. El informe nuevo ya suprime la falsa conclusión de ahorro. Preserva la copia `../actionbench-audit/pilot-002/` y el artefacto original. No cambies el código de la campaña congelada.
- Ya se añadieron contratos de salida estructurada, correcciones de IDs y emparejamiento de skills, ledger de respuestas y costes, auditoría, informe de validez, búsqueda exacta de artefactos y smoke Docker. La batería offline pasa: **45 tests**. Eso no demuestra que el canary real funcione.
- Docker real básico pasó en [run 36664517177](https://github.com/11adyy/actionbench/actions/runs/36664517177). La reanudación de checkpoint entre runners pasó en [run 36664981386](https://github.com/11adyy/actionbench/actions/runs/36664981386); comprueba su artefacto y las invariantes, no te conformes con el check verde.
- Al redactar esto, el [canary generado `canary-001`](https://github.com/11adyy/actionbench/actions/runs/36664726646) seguía ejecutándose desde `83d99e4`; el [smoke Docker ampliado](https://github.com/11adyy/actionbench/actions/runs/36665177418) seguía ejecutándose desde `ec56658`. **Consulta el resultado y los artefactos antes de lanzar nada nuevo**. Aunque se hayan completado, inspecciona los datos: el éxito del workflow por sí solo no cumple las puertas que siguen.
- La clave está en el secret de GitHub `OPENAI_API_KEY`. No muestres ni copies su valor. El modelo de la campaña anterior es `gpt-6-luna`; el techo configurado es 8 USD por campaña. Consulta el gasto confirmado y las reservas en cada artefacto antes de decidir nuevas ejecuciones. No cambies modelo, esfuerzo, precios o techo sin dejar constancia y sin una campaña nueva cuando cambie la configuración experimental.

## 2. Prioridad inmediata: diagnosticar los dos jobs en curso

1. Descarga logs y artefactos de los runs anteriores. Guarda fuera del repositorio los ZIP originales y sus SHA-256. Anota `run_id`, source SHA, configuración, campaña, coste confirmado, reservas, número de peticiones, paquetes generados, revisiones, fallos y estado final.
2. Si falla el smoke ampliado, reproduce el error con el mismo runner de producción (`ContainerRunner`), corrige permisos, timeout o limpieza según la causa, añade prueba de regresión y ejecuta **otro** smoke real desde un commit nuevo. No elimines restricciones de red, memoria, CPU, solo lectura ni la prueba que falló para obtener verde.
3. Si falla el canary, localiza la primera causa raíz en ledger/eventos y distingue: rechazo estructural, error del proveedor, presupuesto, fallo del script, entrada incompatible, fallo del grader o fallo de infraestructura. Corrige el contrato o la implementación. No escribas a mano el paquete que el experimento debe generar. Lanza un canary nuevo con ID nuevo cuando cambie código o configuración; nunca interpretes `canary-001` como si hubiera usado el nuevo commit.
4. Si el canary pasa, verifica una cadena de evidencia concreta por familia: paquete generado y hash, skill emparejada byte-idéntica, procedure invocada en Docker, llamada broker → proveedor con request ID y uso, respuesta de la procedure, y puntuación independiente de al menos una tarea de desarrollo. Si falta un eslabón, el canary es incompleto aunque su JSON diga `passed: true`.

**Entrega de esta fase:** tabla corta con run, commit, prueba, resultado real, coste y enlace al artefacto. Los runs fallidos se conservan.

## 3. Corregir el canary: ahora no valida el experimento completo

`actionbench/commands.py::_canary` inspecciona los tres paquetes y llama directamente a una procedure de script/action. **No ejecuta naturalmente las cinco condiciones ni hace grading de esas cinco decisiones**. La prueba directa de la action es útil como integración, pero no prueba adopción por el agente ni comparación justa.

Implementa un canary separado del test final que haga lo siguiente:

1. Fija previamente un subconjunto de tareas **solo de desarrollo**, con IDs guardados en la configuración/artefacto; una réplica y las cinco condiciones `plain`, `skill`, `skill_script`, `improvised`, `action`. Selección determinista anterior a los resultados; no elegir tareas fáciles tras ver la salida.
2. Usa `AgentRunner`, `ActionRunner`, `Broker`, `ContainerRunner` y `grade` reales. No una función paralela simplificada. Guarda cada decisión, herramienta, llamada interna, respuesta final, resultado de grader y coste bajo un episodio duradero y reanudable.
3. Haz por separado la prueba de integración directa de una action generada. La entrada debe satisfacer su schema y representar una tarea de desarrollo; registra si el código llamó al broker de verdad. El generador puede producir una action semánticamente inútil: documenta ese resultado, no lo ocultes.
4. En el recorrido natural, registra si el agente seleccionó la procedure y cuántas veces. Si no hay invocaciones naturales, informa `adoption=0`; no atribuyas a las actions una mejora de ejecución. Una ablación de uso forzado puede añadirse, pero debe etiquetarse y analizarse por separado.
5. Acepta respuestas erróneas **puntuables** y fallos genuinos del método tipados. Un canary solo bloquea el piloto por defectos técnicos sistémicos: protocolo no consumible, grader roto, broker inaccesible, ausencia de paquetes ejecutables por diseño defectuoso o reanudación insegura. No repitas hasta seleccionar un resultado bonito.

**Aceptación:** por cada familia hay un paquete ejecutable de skill, skill_script y action; una action generada realiza una llamada interna real; hay al menos una evaluación natural de cada condición con trazas y puntuación o fallo genuino tipado; todas las llamadas y costes están en el ledger. El informe separa prueba directa, adopción natural y calidad.

## 4. Tipar los fallos en el punto donde ocurren

El campo `failure_kind` existe, pero `commands.py` agrupa muchas excepciones como `agent_error`; `agent.py` trata `ActionBenchError` de una herramienta como si fuera siempre un error de protocolo reparable. Eso puede cobrar dos reparaciones por un fallo de Docker o de un script y luego convertirlo en cero del método. `report.py` puede declarar `validation_status=passed` si no falta paquete ni hay fallos legacy, aun con fallos técnicos clasificados de otro modo. Corrige esto antes de interpretar resultados.

1. Crea clases o códigos estables para: decisión/JSON inválidos; entrada de procedure inválida; script generado fallido; paquete no disponible; límite de episodio; respuesta completa pero rechazada/incompleta; resultado remoto ambiguo; fallo del broker; fallo del contenedor; fallo del grader; error interno del harness; presupuesto global agotado. Mantén mensaje humano y evidencia original por separado.
2. Define una tabla explícita `recoverable_by_agent`, `counts_as_method_failure`, `invalidates_comparison`, `retryable_after_restart`. **No** deduzcas estas propiedades buscando palabras en `error`.
3. En `AgentRunner` permite máximo dos reparaciones únicamente de decisiones inválidas o errores de herramienta documentadamente reparables. Un script mal generado puede ser fallo del método o recibir una observación para que el agente corrija, pero no lo etiquetes automáticamente como “protocolo”; la política debe ser fija, simétrica y contabilizada.
4. Un error del grader, una excepción inesperada del harness o una pérdida ambigua del proveedor nunca debe transformarse en puntuación cero. Conserva el episodio para diagnóstico/bloqueo. Un paquete generado que falla por su propio código sí puede contar como fallo del método bajo un harness verificado.
5. En el informe, muestra cantidades por clase/familia/condición y **bloquea conclusiones inferenciales** cuando exista contaminación técnica relevante, con la razón exacta. No exijas que action gane; un resultado negativo válido sigue siendo interpretable.

Pruebas de aceptación: inyecta por separado cada clase de fallo; verifica estado del episodio, si se reintenta, coste, denominador y `validation_status`. Incluye un control donde action ejecuta correctamente pero puntúa peor: debe seguir siendo un resultado negativo válido.

## 5. Costes y comparación científica: arreglar el estimando antes del piloto

Hoy `report.py` calcula el coste de reutilización sumando coste de **creación** al coste de test, pero las revisiones se eligen mediante episodios `creation-dev` que consumen dinero. Ese coste de desarrollo forma parte del coste de preparar la action. Además, la comparación de coste por episodio debe especificar qué ocurre si el paquete no se crea o nunca se invoca.

1. Congela dos estimandos distintos. **Por asignación**: calidad y coste total de toda la política action frente a cada baseline, incluyendo fracasos propios de generación bajo harness válido y coste de creación/selección. **Condicional al paquete ejecutable**: análisis secundario de ejecución, claramente etiquetado y nunca usado para esconder tasa de fallo de generación. La adopción natural se reporta aparte.
2. Atribuye a cada paquete/réplica todos los costes de generación, revisiones y validación sobre desarrollo. Reparte ese coste entre un horizonte de usos `H` declarado antes del test; distingue los controles de infraestructura, que no son coste del método. Para `skill_script`, incluye su preparación equivalente; para `skill`, incluye su generación y selección si el estimando compara coste total de políticas. Presenta además coste de inferencia durante test, latencia y número de llamadas.
3. Define qué significa ahorro: margen de no inferioridad de calidad y diferencia de coste total con intervalos. Nunca llames ahorro a cero peticiones causadas por paquete inexistente. Si no hay action ejecutable o adopción, indica qué estimando sigue identificable y cuál no.
4. El diseño contiene dos familias y tres comparaciones principales por familia. Declara **una** comparación primaria y un desenlace primario antes del nuevo test, o ajusta explícitamente la multiplicidad si vas a afirmar seis hallazgos. Los demás contrastes son secundarios/exploratorios. No es válido revisar seis intervalos al 95 % y publicar solo el que sale favorable.
5. Mantén el bootstrap pareado por tarea y réplica de paquete, pero prueba con grids completos, faltantes y fallos técnicos. Reporta `n_tasks`, `n_replicas`, celdas puntuables, fallos del método, exclusiones técnicas y anchura de CI. No equipares 600 episodios con 600 tareas independientes.
6. Un piloto con 20 tareas por familia y tres réplicas estima efectos e incertidumbre de forma exploratoria. Usa `plan-sample` solo si el piloto es técnicamente válido; reserva tareas no expuestas para confirmación. Si no hay tareas suficientes, limita las afirmaciones del paper en vez de fabricar potencia.

**Aceptación:** tres fixtures de informe: (a) no se creó ninguna action ⇒ ninguna afirmación de ahorro operativo; (b) action funciona y es peor ⇒ diferencia negativa conservada; (c) action funciona, calidad no inferior y coste **total** menor tras incluir preparación ⇒ conclusión correspondiente. Recalcula a mano algunas celdas y compara con el informe.

## 6. Reanudación, estados y GitHub Actions

El checkpoint entre runners pasó una prueba básica, pero falta cubrir las ventanas de interrupción que importan para las API pagadas. `cloud/github_worker.sh` todavía puede terminar el job correctamente después de un `freeze` de una campaña técnicamente inválida; esa diferencia debe verse claramente en el workflow. La transición `complete` también necesita más que un marcador: sus metadatos deben concordar con SQLite, configuración, fuente, paquetes, informe y artefacto.

1. Prueba interrupciones reproducibles en: antes de reservar; tras reservar y antes de enviar; tras marcar `submitted` y antes de persistir respuesta; tras persistir respuesta y antes de respuesta final; tras guardar respuesta final y antes del grader; tras guardar evaluación. Usa puntos de fallo explícitos habilitados solo en pruebas, sin alterar la ruta normal.
2. Reanuda en **otro runner** desde el artefacto publicado. Verifica hashes de fuente/manifest/config/imágenes/paquetes, filas del ledger, costes, contador de llamadas y ausencia de duplicación de episodios o solicitudes confirmadas. La ventana tras envío y antes de guardar respuesta debe quedar **bloqueada**, no reintentada a ciegas. Documenta el procedimiento manual de reconciliación y pruébalo con evidencia controlada.
3. Prueba el presupuesto global: deja episodios pendientes y artefacto reanudable; muestra coste confirmado y reserva incierta. No los conviertas en ceros ni incrementes presupuesto automáticamente.
4. Si fallan generación del informe, verificación de hashes o grader, el job debe marcar fallo técnico y aun así subir el estado durable con `always()`. Si el experimento termina con un resultado negativo válido, el job puede terminar verde; el resumen debe decir “resultado negativo”, no “harness validado” por inferencia.
5. El `GITHUB_STEP_SUMMARY`, `status.json`, `report.json`, SQLite y marcador final deben concordar. Añade en el resumen tareas puntuadas/planificadas, fallos técnicos y del método, paquetes creados/usados, gasto confirmado/reservado, validez y enlaces al artefacto. Verifica que la recuperación de artefactos es exacta y paginada también cuando existen muchos runs con prefijos similares.

## 7. Nueva campaña y criterios de salida

Ejecuta en orden, guardando para cada puerta run, commit, campaña, comando, resultado y artefacto: **A)** 45+ regresiones offline; **B)** Docker real completo, incluidos timeout y limpieza; **C)** API real con decisión estructurada y action de control; **D)** canary generado y evaluación natural de cinco condiciones; **E)** reanudación e inyección de fallos; **F)** nuevo piloto completo. No lances F mientras A–E tengan fallos técnicos abiertos.

F congela por adelantado modelo, esfuerzo, precios, manifest, split, código, imágenes, condiciones, horizonte de reutilización, presupuesto, comparación primaria, criterio de no inferioridad y política de fallos. Usa un ID nuevo y el mismo modelo inicial para aislar la reparación del harness. Los datos de `pilot-002` ya se usaron para depurar y no sirven como confirmación independiente. El piloto puede ser exploratorio; si sigue sin paquetes action o sin uso natural, informa esa limitación sin vender ahorro.

Al acabar F, entrega: matriz planificado/puntuado/fallos por condición; paquetes generados, ejecutables y usados; gasto por fase con reservas; calidad y coste pareados; intervalos y límites de interpretación; todos los runs y artefactos. Incluye una sección “Qué **no** demuestra este estudio” (dos familias, un modelo, pocas réplicas, posibles efectos del prompt/harness). Para el paper confirmatorio, prepara después un protocolo prospectivo y tareas nuevas; no llames confirmatorio al piloto reparado.

## 8. Forma de trabajo exigida

- Antes de editar: inspecciona estado actual de GitHub y corre el test base. Tras cada corrección: test específico, suite offline y, si afecta Docker/API/estado, gate real nuevo desde commit nuevo.
- Un commit coherente por causa o subsistema. Sube el historial; no reescribas campañas ni commits publicados para ocultar fallos.
- No uses mocks para probar que el modelo generó paquetes, que una action llamó al proveedor, que Docker aisló código o que el grader oficial puntuó. Los mocks son válidos **solo** para unit tests de estados, parsing, presupuestos e inyección controlada.
- No publiques claves, respuestas privadas extensas, URLs firmadas de artefactos ni trazas con datos sensibles. El ledger original y las ejecuciones fallidas son evidencia, no basura que se elimina.
- Si algo no pasa, registra el defecto concreto, el alcance científico y el siguiente paso. No marques la tarea como terminada solo porque el workflow está verde.

**Definición de terminado:** una persona puede iniciar con su propia API key una campaña con paquetes realmente generados, ver una action llamando al modelo dentro de Docker por el broker, obtener puntuaciones independientes de las cinco condiciones, detenerla y reanudarla sin repetir llamadas confirmadas, y leer un informe que distingue fallo del método, fallo técnico, calidad, coste completo y alcance estadístico. Solo entonces hay infraestructura suficiente para poner a prueba la idea; una afirmación científica favorable requiere además datos adecuados.
