import type { CartMandate, IntentMandate, OrderResult } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

function Step({
  index,
  title,
  status,
  children,
}: {
  index: number;
  title: string;
  status: "done" | "pending" | "failed";
  children?: React.ReactNode;
}) {
  const dotTone =
    status === "done" ? "bg-ok" : status === "failed" ? "bg-err" : "bg-text-faint";
  return (
    <div className="flex gap-3">
      <div className="flex flex-col items-center">
        <div
          className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold text-black ${dotTone}`}
        >
          {index}
        </div>
        <div className="mt-1 w-px flex-1 bg-border-soft" />
      </div>
      <div className="min-w-0 flex-1 pb-5">
        <div className="text-sm font-medium">{title}</div>
        {children}
      </div>
    </div>
  );
}

export function MandateFlow({
  intentMandate,
  cartMandate,
  order,
}: {
  intentMandate: IntentMandate | null;
  cartMandate: CartMandate | null;
  order: OrderResult | null;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg-panel p-5">
      <h3 className="text-sm font-semibold text-text-dim">AP2 mandate chain</h3>
      <div className="mt-4">
        <Step
          index={1}
          title="Intent Mandate"
          status={intentMandate ? "done" : "pending"}
        >
          {intentMandate ? (
            <dl className="mt-1.5 grid grid-cols-1 gap-x-4 gap-y-1 font-mono text-xs text-text-dim sm:grid-cols-2">
              <div>
                <dt className="text-text-faint">mandate_id</dt>
                <dd className="truncate text-text">{intentMandate.mandate_id}</dd>
              </div>
              <div>
                <dt className="text-text-faint">max_amount</dt>
                <dd className="text-text">
                  {intentMandate.currency} {intentMandate.max_amount.toFixed(2)}
                </dd>
              </div>
              <div className="col-span-full">
                <dt className="text-text-faint">signature</dt>
                <dd className="truncate text-text">{intentMandate.signature}</dd>
              </div>
            </dl>
          ) : (
            <p className="mt-1 text-xs text-text-faint">
              Signed by the principal, authorizing the agent to act.
            </p>
          )}
        </Step>

        <Step
          index={2}
          title="Cart Mandate"
          status={order ? "done" : "pending"}
        >
          {cartMandate ? (
            <>
              <p className="mt-1 text-xs text-text-faint">
                Binds the authorization to this exact SKU, quote and amount --
                re-verified before settlement.
              </p>
              <dl className="mt-1.5 grid grid-cols-1 gap-x-4 gap-y-1 font-mono text-xs text-text-dim sm:grid-cols-2">
                <div>
                  <dt className="text-text-faint">mandate_id</dt>
                  <dd className="truncate text-text">{cartMandate.mandate_id}</dd>
                </div>
                <div>
                  <dt className="text-text-faint">amount</dt>
                  <dd className="text-text">
                    {cartMandate.currency} {cartMandate.amount.toFixed(2)}
                  </dd>
                </div>
                <div>
                  <dt className="text-text-faint">sku</dt>
                  <dd className="truncate text-text">{cartMandate.sku}</dd>
                </div>
                <div>
                  <dt className="text-text-faint">quote_id</dt>
                  <dd className="truncate text-text">{cartMandate.quote_id}</dd>
                </div>
                <div className="col-span-full">
                  <dt className="text-text-faint">signature</dt>
                  <dd className="truncate text-text">{cartMandate.signature}</dd>
                </div>
              </dl>
            </>
          ) : (
            <p className="mt-1 text-xs text-text-faint">
              {order
                ? "Composed and signed by the gateway from its own view of the offer, then re-verified before settlement."
                : "Signed by the gateway once payment is authorized -- never accepts a cart the agent composed itself."}
            </p>
          )}
        </Step>

        <Step
          index={3}
          title="Settlement"
          status={order ? (order.status === "SETTLED" ? "done" : "failed") : "pending"}
        >
          {order ? (
            <div className="mt-1.5 space-y-2">
              <StatusBadge tone={order.status === "SETTLED" ? "ok" : "err"}>
                {order.status}
              </StatusBadge>
              <dl className="grid grid-cols-1 gap-x-4 gap-y-1 font-mono text-xs text-text-dim sm:grid-cols-2">
                <div>
                  <dt className="text-text-faint">rail</dt>
                  <dd className="text-text">{order.rail ?? "--"}</dd>
                </div>
                <div>
                  <dt className="text-text-faint">order_id</dt>
                  <dd className="truncate text-text">{order.order_id}</dd>
                </div>
                <div className="col-span-full">
                  <dt className="text-text-faint">settlement_ref</dt>
                  <dd className="truncate text-text">{order.settlement_ref || "--"}</dd>
                </div>
                {order.reason_code && (
                  <div className="col-span-full">
                    <dt className="text-text-faint">reason_code</dt>
                    <dd className="text-err">{order.reason_code}</dd>
                  </div>
                )}
              </dl>
            </div>
          ) : (
            <p className="mt-1 text-xs text-text-faint">
              Settles on a card network or the x402 stablecoin rail.
            </p>
          )}
        </Step>
      </div>
    </div>
  );
}
