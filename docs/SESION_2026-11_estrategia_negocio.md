# Estrategia de negocio GeoMB — Noviembre 2026

> Análisis brutalmente honesto (pedido explícito del usuario) sobre monetización B2C vs. pivot
> B2B, hecho con datos reales de facturación/retención tras la auditoría de AWS de esta sesión.
> Pendiente de retomar: aquí quedan las conclusiones y las siguientes acciones de validación.

## Estado real al momento del análisis

- **Costos AWS:** ~$26 USD/mes (~$520 MXN) — $12.50 USD fijo (EC2+VPC) + $13.50 USD variable
  (122 GB data transfer + fracción mínima de Polly/TTS).
- **Tracción:** 668 instalaciones activas en Google Play.
- **Retención:** 5% al día 28 (~33 usuarios activos de esa cohorte).
- **Señal B2B orgánica:** 3 supervisores operativos de rutas dependen de un parche de servidor
  local porque la app les es indispensable para su trabajo — sin pago, sin que se les pidiera.
- Se descubrió que la voz (TTS) cuesta ~$2 USD/mes gracias al caché — la premisa original de que
  "voz en el recorrido" era una función cara que justificaba cobrar quedó descartada.

## Plan original (descartado en esta sesión)

Suscripción de $50 MXN/mes al pasajero general, desbloqueando "voz en el recorrido",
"asistente virtual de IA" y "personalización visual".

## Diagnóstico B2C (por qué se descarta)

- Cubrir solo el costo de AWS requiere **13 suscriptores pagando cada mes, para siempre**
  (520 MXN / 50 MXN, más la comisión de Google Play ~15%) — sin margen, sin colchón, sin pagar
  el tiempo del desarrollador.
- El pool de donde saldrían esos suscriptores es optimista: 33 usuarios retenidos a día 28.
  Con una conversión freemium realista (1-5% para apps de utilidad, no entretenimiento), eso da
  ~1-2 personas pagando — ni cerca de 13.
- 5% de retención a día 28 no es "le faltan funciones": es una app de **uso de baja frecuencia
  por diseño de categoría** (un pasajero aprende su ruta en pocos viajes y deja de abrir la app).
  Eso no se arregla agregando funciones, se arregla cambiando de público.
- **Asistente virtual de IA:** mismo error de cálculo que ya se cometió con la voz, pero sin el
  salvavidas del caché — costo variable por conversación, sin tope, sin usuarios que paguen por
  costearlo. No ataca la causa real de la fuga.
- **Personalización visual:** nadie paga por estética en una app de transporte; esa categoría se
  monetiza por certeza y tiempo ahorrado.

**Veredicto: suscripción B2C a pasajeros, con esas funciones, matemáticamente muerta.**

## El pivot B2B (validado parcialmente, falta el paso clave)

- La señal es real: 3 personas dependen de la herramienta para su trabajo diario bajo condiciones
  peores (servidor local) que las del B2C — eso vale más que 668 instalaciones indiferentes.
- **Hueco de validación pendiente (crítico, no saltarse):** no se ha preguntado directamente a
  esos supervisores, ni a su empresa concesionaria, si PAGARÍAN por esto. Que un empleado use la
  herramienta gratis no implica que su empresa autorice un gasto recurrente.
- Falta dimensionar el mercado total: ¿cuántas empresas concesionarias existen en todo el sistema
  (Metrobús/Mexibús/Mexicable)? Determina si B2B es un negocio o solo 3 clientes para siempre.
- Riesgo operativo ya presente: se está dando soporte no pagado a esos 3 usuarios (el servidor
  local lo confirma). Si crece el uso B2B sin resolver pago antes, el riesgo es quedar obligado a
  disponibilidad 24/7 gratis porque su trabajo depende de la app.

## Qué construir para B2B (si se valida)

La ventaja: el pipeline de tiempo real y afectaciones YA EXISTE (`ManifestacionesService`,
`AfectMexibusFeed`, `AfectacionesMexibus`, `detector_proximidad.py`). Para B2B no hace falta
construir de cero — hace falta **empacar datos que ya se generan**: bitácora de afectaciones por
ruta, tiempos de recorrido real vs. programado, alertas de bloqueo con historial, exportables.
Costo marginal casi cero comparado con el asistente de IA.

**Precio:** no inventar otro número (como pasó con los $50 MXN) — preguntar directamente a los
supervisores/su empresa cuánto vale para su operación. Como referencia de mercado: SaaS operativo
B2B para herramientas que afectan la operación diaria suele cotizarse por empresa o por ruta, en
órdenes de cientos a miles de MXN/mes, no en decenas por usuario individual.

## Costo de oportunidad de no decidir

Un solo desarrollador, con deudas y el servidor en riesgo, no tiene tiempo de servir bien a dos
públicos con necesidades opuestas (pasajeros que quieren gratis y bonito vs. operadores que
quieren confiable y pagan). Seguir repartido no termina en "fracaso parejo" — termina perdiendo
tiempo en B2C sin ingreso mientras el riesgo del servidor eventualmente cuesta la confianza de los
3 clientes que sí tenían intención real de pagar.

## Recomendación y próximos pasos (pendientes)

1. Pausar toda monetización B2C: nada de suscripción, nada de asistente de IA, por ahora.
2. **Acción crítica pendiente:** tener la conversación de precio con los supervisores y/o su
   empresa concesionaria — validar disposición real de pago antes de construir nada nuevo.
3. Resolver situación fiscal (RFC / régimen para recibir pagos recurrentes) en paralelo — se
   necesita de cualquier forma, sea cual sea el modelo que gane.
4. No escribir código de monetización (ni B2C ni B2B) hasta tener un número real de esa
   conversación.

*(Nota: nunca se llegó a resolver la pregunta pendiente de sesiones anteriores sobre "permanente
vs. mensual" como forma de pago — queda igual de abierta, ahora subordinada a si el pivot B2B se
valida.)*
