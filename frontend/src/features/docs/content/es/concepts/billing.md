Skynet usa un saldo prepago. Añades fondos al saldo. Cada ejecución tiene un límite de gasto, y Skynet cobra a tu saldo el uso real de la ejecución. Esta página explica el saldo, los presupuestos de ejecución, las reservas, las tarifas y tus propias claves de proveedor.

> [!NOTE]
> Las cifras de esta página son los valores predeterminados actuales. La aplicación muestra los valores en vigor. Abre **Ajustes → Facturación** para ver tu saldo, la tarifa de recarga y tu historial de transacciones.

## Saldo

Tu saldo está en dólares estadounidenses. Skynet lo guarda en centavos.

Por defecto, Skynet no da ningún saldo inicial gratuito. Añade fondos antes de iniciar tu primera ejecución de pago.

Añades fondos en **Ajustes → Facturación**. Puedes elegir una cantidad fija o introducir una cantidad personalizada:

| Recarga               | Cantidad añadida a tu saldo     |
| --------------------- | ------------------------------- |
| Cantidades fijas      | 5 $, 20 $ o 50 $                |
| Personalizada         | Cualquier cantidad de 5 $ a 1.000 $ |

La cantidad que eliges es la cantidad que entra en tu saldo. La tarifa de recarga se suma a ella. Consulta las tarifas más abajo.

**Ajustes → Uso** muestra lo que gastaste. El historial de transacciones de **Ajustes → Facturación** enumera tus recargas y los cargos de las ejecuciones.

## Presupuestos de ejecución

Cada ejecución tiene un límite de gasto. El límite es lo máximo que puede gastar la ejecución. Cubre la configuración y la optimización.

Una ejecución no tiene una estimación de coste antes de empezar. Gasta hasta su límite, se detiene al llegar al presupuesto y conserva su mejor versión. Lee [Ejecuciones](/docs/concepts/runs) para ver cómo decide Skynet cuándo detenerse.

El límite se fija en la pantalla **Presupuesto** del asistente de envío, en el campo **Límite de gasto**:

- **Fijar un límite** es el modo predeterminado. El límite empieza en 5 $. Si la ejecución necesita más de 5 $ para empezar, el límite empieza en esa cantidad.
- **Sin límite** permite que la ejecución use tu saldo hasta que termine. La ejecución nunca se pausa. Si tu saldo se agota, la ejecución se detiene y conserva lo que produjo.

### La cantidad necesaria para empezar

El asistente muestra la cantidad **Necesario para empezar**. Esta cantidad es:

- las retenciones de los sandboxes que la ejecución abre antes de su primera llamada al modelo, más
- un margen para las primeras llamadas al modelo. El margen es de 1 $, o el coste de la llamada individual más cara de los modelos que seleccionaste si es mayor.

El asistente no te deja continuar cuando el límite está por debajo de esta cantidad. También te detiene cuando tu saldo está por debajo de esta cantidad. Sube el límite o añade fondos para continuar.

> [!TIP]
> Cuando una ejecución se detiene al llegar a su presupuesto, usa **Subir el límite y continuar** en la página de la ejecución. La ejecución continúa desde su último punto de control.

## Reservas

Antes de que Skynet haga trabajo de pago, reserva una parte del límite de la ejecución. Skynet libera la reserva cuando el trabajo se liquida y cobra solo el coste real.

Cuando un presupuesto ya tiene actividad, la tarjeta **Límite de gasto** del asistente muestra su libro de cuentas:

| Etiqueta                       | Significado                                                                   |
| ------------------------------ | ----------------------------------------------------------------------------- |
| **Restante en este límite**    | La parte del límite que queda.                                                |
| **Gastado hasta ahora**        | El coste ya liquidado.                                                        |
| **Reservado**                  | Dinero retenido para el trabajo en curso. Skynet lo libera cuando el trabajo se liquida. |

## Tarifas

Skynet aplica dos cargos además del coste del proveedor.

| Cargo                                    | Valor predeterminado actual                  | Se aplica a                                              |
| ---------------------------------------- | -------------------------------------------- | -------------------------------------------------------- |
| Tarifa de recarga                        | 12,5 % de la cantidad, más 0,35 $            | Cada recarga                                             |
| Recargo por uso                          | Coste del proveedor × 1,15 (coste más 15 %)  | Llamadas al modelo con las claves de Skynet, y cómputo del sandbox |

### Tarifa de recarga

Skynet redondea la parte porcentual al centavo superior y luego suma 0,35 $. Por ejemplo, una recarga de 20 $ tiene una tarifa de 2,85 $. La pantalla de recarga muestra la tarifa antes de que pagues.

### Recargo por uso

Skynet multiplica el coste del proveedor por el recargo por uso. El recargo se aplica a las llamadas al modelo que paga Skynet y al cómputo del sandbox. Las reservas, la cantidad necesaria para empezar y los precios de los modelos en la aplicación ya incluyen el recargo.

Skynet obtiene el coste del proveedor de una llamada al modelo en este orden:

1. El coste que OpenRouter informa para la llamada.
2. La lista de precios en vigor de OpenRouter para el modelo.
3. La tabla de precios de LiteLLM.
4. Una tarifa alta de respaldo de 15 $ por millón de tokens de entrada y 75 $ por millón de tokens de salida. Esta tarifa garantiza que Skynet nunca cobre un modelo desconocido por debajo de su coste.

## Tus propias claves de proveedor (BYOK)

Puedes ejecutar llamadas al modelo con tu propia clave de proveedor. Añade claves en **Ajustes → Proveedores**. Para OpenRouter, también puedes usar **Continuar con OpenRouter**.

Cuando una llamada al modelo usa tu clave:

- Tu proveedor cobra los tokens del modelo a tu clave.
- Skynet no cobra ninguna tarifa por el modelo.
- El presupuesto de la ejecución solo cubre el uso del entorno aislado.

Las tarifas de un evaluador externo o de un proveedor MCP van directamente a ese proveedor. No forman parte del límite de gasto de la ejecución.

## Llamadas al modelo desde tu código

El código de tu repositorio y tu evaluador pueden llamar a un modelo a través de la pasarela de modelos de Skynet. Lee [Cómo funciona la optimización](/docs/concepts/optimization).

- Una llamada con las claves de Skynet se mide contra el presupuesto de la ejecución, como cualquier otra llamada al modelo. Se aplica el recargo por uso.
- Una llamada con tu propia clave (BYOK) va a la clave de tu proveedor. Skynet no cobra ninguna tarifa por el modelo.

## Uso del sandbox

El código de tu repositorio y tu evaluador se ejecutan en un sandbox. Skynet mide el cómputo del sandbox:

1. Antes de que arranque un sandbox, Skynet reserva una cantidad del límite de la ejecución.
2. Cuando el sandbox termina, Skynet cobra el uso final que informa el proveedor del sandbox.
3. El recargo por uso se aplica al cómputo del sandbox.

El cómputo del sandbox forma parte del límite de gasto de la ejecución, también cuando las llamadas al modelo usan tu propia clave.

## Servicio

Cada llamada al endpoint de servicio de una ejecución terminada se factura por separado. Quien llama paga con su propio saldo. Define `max_cost_cents` en la solicitud para limitar una llamada. Lee [Resultados](/docs/guides/results).

## Planes

Hay dos planes: Free y Pro. Pro amplía algunos límites, como las ejecuciones simultáneas y el almacenamiento. Si falla la renovación de Pro, la cuenta conserva los límites de Pro durante 7 días. Después usa los límites de Free hasta que el pago se complete. Consulta [Límites](/docs/limits).

## Protecciones de la plataforma

- Un turno interactivo del modelo, como un chat con el asistente de IA, necesita un saldo de al menos 0,05 $ antes de empezar.
- El asistente de IA del asistente de envío es gratuito. Cada cuenta puede iniciar 50 turnos de este asistente de IA por día UTC.

## Páginas relacionadas

- [Ejecuciones](/docs/concepts/runs)
- [Iniciar una ejecución con el asistente](/docs/guides/submit)
- [Límites](/docs/limits)
