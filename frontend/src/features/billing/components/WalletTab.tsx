"use client";

import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { EmptyState } from "@/shared/ui/empty-state";
import * as React from "react";
import { motion } from "framer-motion";
import {
  ArrowSquareOut,
  CircleNotch,
  ClockCounterClockwise,
  CreditCard,
  PencilSimple,
  Plus,
  Sparkle,
  Trash,
  UserCircle,
  Wallet,
} from "@/shared/ui/icons";
import { toast } from "react-toastify";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { track, TelemetryEvent } from "@/shared/lib/telemetry";
import { cn } from "@/shared/lib/utils";
import { useLocale } from "@/shared/providers";
import { SettingsRow } from "@/shared/ui/settings-row";
import { Button } from "@/shared/ui/primitives/button";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { StatusPill } from "@/shared/ui/status-badge";
import { Badge } from "@/shared/ui/primitives/badge";
import { Dialog, DialogContent, DialogFooter } from "@/shared/ui/primitives/dialog";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { Switch } from "@/shared/ui/primitives/switch";
import { DialogTitleRow } from "@/shared/ui/dialog-title-row";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import {
  createBillingPortalSession,
  createCheckoutSession,
  getBillingProfile,
  getBillingTransactions,
  removePaymentMethod,
  updatePaymentMethod,
  type BillingAddressResponse,
  type BillingProfileResponse,
  type BillingTransaction,
  type BillingTransactionsResponse,
} from "@/shared/lib/api";
import { onLinkChange } from "@/shared/hooks/use-linked-account";
import { LinkAccountPrompt } from "./LinkAccountPrompt";
import { useBalance } from "../providers/balance-provider";
import {
  TOP_UP_PACKS,
  CUSTOM_CENTS_MAX,
  CUSTOM_CENTS_MIN,
  formatCentsUsd,
  formatResetDate,
  formatUsd,
  purchaseFeeUsd,
  purchaseTotalUsd,
  type TopUpPack,
} from "../lib/wallet";

// Slide transition for the top-up pack selector's shared-layout pill — matches the
// runs-source segmented control in explore/SearchBar so the two read identically.
const PILL_TRANSITION = { type: "tween", duration: 0.18, ease: [0.22, 1, 0.36, 1] } as const;

const TRANSACTION_STATUS_LABEL: Record<BillingTransaction["status"], MessageKey> = {
  paid: "billing.transactions.status.paid",
  processing: "billing.transactions.status.processing",
  refunded: "billing.transactions.status.refunded",
  partially_refunded: "billing.transactions.status.partially_refunded",
  disputed: "billing.transactions.status.disputed",
};

/** Whole-dollar USD (no cents) for the buy button — "$20", not "$20.00". */
function formatUsdWhole(usd: number, locale: string): string {
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(usd);
}

/**
 * Inline top-up — one pill control holding the pack segments, a
 * free-type custom amount, and the buy action, all in the segmented-control
 * style. Lives directly in the wallet tab now that the standalone /upgrade
 * page is gone: a prepaid balance is the only plan, so buying is a settings
 * row, not a pricing page.
 */
function AddFundsControls() {
  const { locale } = useLocale();
  const [selection, setSelection] = React.useState<string>(
    () => (TOP_UP_PACKS.find((p) => p.popular) ?? TOP_UP_PACKS[0]!).id,
  );
  const [customDraft, setCustomDraft] = React.useState("");
  const [buying, setBuying] = React.useState(false);
  const { wallet } = useBalance();

  const pack: TopUpPack | undefined = TOP_UP_PACKS.find((p) => p.id === selection);
  // The custom field is typed in whole dollars; the checkout API is denominated
  // in cents.
  const customCents = Number(customDraft || "0") * 100;
  const customValid = customCents >= CUSTOM_CENTS_MIN && customCents <= CUSTOM_CENTS_MAX;
  // The buy button quotes what the buyer is charged: top-up amount plus the
  // backend's platform fee, itemized as its own line on Stripe checkout. The
  // row description names the fee up front, so the first price shown is all-in.
  const cents = pack ? pack.cents : customCents;
  const usd = purchaseTotalUsd(cents, wallet.pricing);
  const priceLabel = Number.isInteger(usd) ? formatUsdWhole(usd, locale) : formatUsd(usd, locale);

  const onBuy = async () => {
    setBuying(true);
    track(TelemetryEvent.CheckoutStarted, {
      pack_id: pack ? pack.id : "custom",
      cents: pack ? pack.cents : customCents,
    });
    try {
      const { url } = await createCheckoutSession(
        pack ? { packId: pack.id } : { cents: customCents },
      );
      window.location.assign(url);
    } catch {
      setBuying(false);
      toast.error(msg("billing.checkout.error"));
    }
  };

  const customActive = pack === undefined;
  const description =
    customActive && !customValid
      ? formatMsg("billing.plans.topup.custom_range", {
          p1: formatUsdWhole(CUSTOM_CENTS_MIN / 100, locale),
          p2: formatUsdWhole(CUSTOM_CENTS_MAX / 100, locale),
        })
      : formatMsg("billing.plans.topup.fee_note", {
          p1: formatUsd(purchaseFeeUsd(cents, wallet.pricing), locale),
        });
  return (
    <SettingsRow icon={Sparkle} label={msg("billing.action.add_funds")} description={description}>
      <div
        role="group"
        aria-label={msg("billing.plans.topup.pack_aria")}
        className="relative flex w-full max-w-full flex-wrap items-center gap-0.5 rounded-lg bg-muted p-0.5 sm:w-auto sm:flex-nowrap"
      >
        {TOP_UP_PACKS.map((p) => {
          const active = p.id === selection;
          return (
            <button
              key={p.id}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => setSelection(p.id)}
              className={cn(
                "relative rounded-md px-2.5 py-1 text-xs font-medium tabular-nums transition-colors duration-200 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45",
                active ? "text-foreground" : "text-foreground/60 hover:text-foreground",
              )}
            >
              {/* Shared-layout pill slides between segments instead of the selected
                background snapping — mirrors the runs-source segmented control. */}
              {active && (
                <motion.span
                  layoutId="credit-pack-pill"
                  className="absolute inset-0 rounded-md bg-background"
                  transition={PILL_TRANSITION}
                  aria-hidden="true"
                />
              )}
              <span dir="ltr" className="relative z-10">
                {formatUsdWhole(p.usd, locale)}
              </span>
            </button>
          );
        })}
        {/* Custom amount is a fourth, free-type segment: focusing or typing makes
          it the active selection and the pill slides behind it. */}
        <span className="relative">
          {customActive && (
            <motion.span
              layoutId="credit-pack-pill"
              className="absolute inset-0 rounded-md bg-background"
              transition={PILL_TRANSITION}
              aria-hidden="true"
            />
          )}
          <input
            value={customDraft}
            onChange={(event) => {
              setCustomDraft(event.target.value.replace(/\D/g, ""));
              setSelection("custom");
            }}
            onFocus={() => setSelection("custom")}
            inputMode="numeric"
            maxLength={4}
            dir="ltr"
            placeholder={msg("billing.plans.topup.custom")}
            aria-label={msg("billing.plans.topup.custom_amount_aria")}
            className={cn(
              "relative z-10 h-[44px] w-16 rounded-md bg-transparent px-2.5 py-1 text-center text-xs font-medium tabular-nums outline-none transition-colors duration-200 placeholder:font-normal placeholder:text-muted-foreground/90 lg:h-auto [@media(hover:none)_and_(pointer:coarse)]:h-[44px]",
              customActive ? "text-foreground" : "text-foreground/60",
            )}
          />
        </span>
        <span
          aria-hidden="true"
          className="mx-0.5 hidden h-4 w-px shrink-0 bg-border/70 sm:block"
        />
        <Button
          size="sm"
          onClick={onBuy}
          data-telemetry="wallet-buy-credits"
          data-accent
          disabled={buying || (!pack && !customValid)}
          className="h-[44px] rounded-full px-2.5 text-[0.6875rem] font-semibold sm:h-6 [@media(hover:none)_and_(pointer:coarse)]:h-[44px] [&_svg:not([class*='size-'])]:size-3"
        >
          {buying ? (
            <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <Sparkle aria-hidden="true" />
          )}
          {formatMsg("billing.upgrade.buy", { p1: priceLabel })}
        </Button>
      </div>
    </SettingsRow>
  );
}

/** Collapse a Stripe billing address into a compact, locale-safe display line. */
function formatAddress(address: BillingAddressResponse): string {
  return [
    address.line1,
    address.line2,
    [address.city, address.state, address.postal_code].filter(Boolean).join(" "),
    address.country,
  ]
    .filter(Boolean)
    .join(", ");
}

/** Format a Stripe minor-unit amount in its declared currency. */
function formatTransactionAmount(amount: number, currency: string, locale: string): string {
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency: currency.toUpperCase(),
    }).format(amount / 100);
  } catch {
    return `${(amount / 100).toFixed(2)} ${currency.toUpperCase()}`;
  }
}

/** Render Stripe purchase history within the billing settings tab. */
function TransactionHistory() {
  const { locale } = useLocale();
  const [data, setData] = React.useState<BillingTransactionsResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [failed, setFailed] = React.useState(false);

  const load = React.useCallback(() => {
    setLoading(true);
    setFailed(false);
    getBillingTransactions()
      .then(setData)
      .catch(() => setFailed(true))
      .finally(() => setLoading(false));
  }, []);

  React.useEffect(() => {
    load();
  }, [load]);

  React.useEffect(() => onLinkChange(load), [load]);

  return (
    <section
      className="flex flex-col gap-2 border-t border-border/40 pt-5"
      aria-labelledby="transaction-history-heading"
    >
      <div className="flex items-center justify-between gap-3">
        <h3 id="transaction-history-heading" className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <ClockCounterClockwise className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          {msg("billing.transactions.title")}
        </h3>
        {loading && data != null && (
          <CircleNotch className="size-3.5 animate-spin text-muted-foreground" aria-hidden="true" />
        )}
      </div>
      {failed ? (
        <div className="flex items-center justify-between gap-3 border-y border-border/40 py-3">
          <span className="text-xs text-muted-foreground">
            {msg("billing.transactions.load_error")}
          </span>
          <RetryIconButton label={msg("billing.wallet.retry")} onClick={load} />
        </div>
      ) : data == null ? (
        <div
          className="flex h-20 items-center justify-center border-y border-border/40"
          aria-busy="true"
        >
          <CircleNotch className="size-4 animate-spin text-muted-foreground" aria-hidden="true" />
        </div>
      ) : data.entries.length === 0 ? (
        <EmptyState
          variant="list"
          icon={CreditCard}
          title={
            data.available
              ? msg("billing.transactions.empty")
              : msg("billing.transactions.unavailable")
          }
          className="border-y border-border/40"
        />
      ) : (
        <ul className="divide-y divide-border/40 border-y border-border/40">
          {data.entries.map((transaction) => {
            const statusTone =
              transaction.status === "paid"
                ? "success"
                : transaction.status === "processing"
                  ? "running"
                  : "failed";
            return (
              <li key={transaction.id} className="flex flex-wrap items-center gap-3 py-3">
                <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
                  <CreditCard className="size-4" aria-hidden="true" />
                </span>
                <span className="flex min-w-36 flex-1 flex-col gap-0.5">
                  <span className="text-sm font-medium text-foreground">
                    {transaction.cents == null
                      ? msg("billing.transactions.purchase")
                      : formatMsg("billing.transactions.balance", {
                          p1: formatCentsUsd(transaction.cents, locale),
                        })}
                  </span>
                  <span dir="ltr" className="text-xs text-muted-foreground">
                    {formatResetDate(transaction.at, locale)}
                  </span>
                </span>
                <span className="ms-11 flex w-[calc(100%_-_2.75rem)] min-w-0 items-center justify-between gap-2 sm:ms-auto sm:w-auto sm:shrink-0 sm:justify-start">
                  <StatusPill tone={statusTone}>
                    {msg(TRANSACTION_STATUS_LABEL[transaction.status])}
                  </StatusPill>
                  <span dir="ltr" className="text-sm font-semibold tabular-nums text-foreground">
                    {formatTransactionAmount(transaction.amount, transaction.currency, locale)}
                  </span>
                  {transaction.document_url && (
                    <Button
                      asChild
                      variant="ghost"
                      size="icon-sm"
                      className="text-muted-foreground hover:text-foreground"
                      autoTooltip={false}
                      aria-label={msg("billing.transactions.receipt")}
                    >
                      <a href={transaction.document_url} target="_blank" rel="noreferrer">
                        <ArrowSquareOut className="size-4" aria-hidden="true" />
                      </a>
                    </Button>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

type PaymentMethod = BillingProfileResponse["payment_methods"][number];

/** "Visa •••• 4242" — the label a payment method goes by in dialogs. */
function paymentMethodLabel(method: PaymentMethod): string {
  const name = method.brand
    ? method.brand.charAt(0).toUpperCase() + method.brand.slice(1)
    : method.type.replaceAll("_", " ");
  return method.last4 ? `${name} •••• ${method.last4}` : name;
}

/** Edit a saved card's holder name and expiry, or make it the default. */
function EditPaymentMethodDialog({
  method,
  onClose,
  onSaved,
}: {
  method: PaymentMethod | null;
  onClose: () => void;
  onSaved: (profile: BillingProfileResponse) => void;
}) {
  const [holderName, setHolderName] = React.useState("");
  const [month, setMonth] = React.useState("");
  const [year, setYear] = React.useState("");
  const [makeDefault, setMakeDefault] = React.useState(false);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (method == null) return;
    setHolderName(method.holder_name ?? "");
    setMonth(method.exp_month != null ? String(method.exp_month) : "");
    setYear(method.exp_year != null ? String(method.exp_year) : "");
    setMakeDefault(false);
    setBusy(false);
  }, [method]);

  if (method == null) return null;

  const isCard = method.type === "card";
  const monthNumber = Number(month);
  const yearNumber = Number(year);
  const thisYear = new Date().getFullYear();
  const expiryValid =
    !isCard ||
    (Number.isInteger(monthNumber) &&
      monthNumber >= 1 &&
      monthNumber <= 12 &&
      Number.isInteger(yearNumber) &&
      yearNumber >= thisYear &&
      yearNumber <= thisYear + 30);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (method == null || !expiryValid) return;
    const changes: Parameters<typeof updatePaymentMethod>[1] = {};
    if (holderName.trim() !== (method.holder_name ?? "")) changes.holder_name = holderName.trim();
    if (isCard && (monthNumber !== method.exp_month || yearNumber !== method.exp_year)) {
      changes.exp_month = monthNumber;
      changes.exp_year = yearNumber;
    }
    if (makeDefault) changes.make_default = true;
    if (Object.keys(changes).length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    try {
      onSaved(await updatePaymentMethod(method.id, changes));
      toast.success(msg("billing.payment_methods.updated"));
      onClose();
    } catch {
      setBusy(false);
      toast.error(msg("billing.payment_methods.update_error"));
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent
        data-settings-text-buttons
        className="w-[min(28rem,92vw)] max-w-[min(28rem,92vw)] sm:max-w-md"
      >
        <DialogTitleRow
          title={msg("billing.payment_methods.edit_title")}
          description={<span dir="ltr">{paymentMethodLabel(method)}</span>}
        />
        <form onSubmit={save} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="payment-method-holder" className="text-xs text-muted-foreground">
              {msg("billing.payment_methods.holder_name")}
            </Label>
            <Input
              id="payment-method-holder"
              value={holderName}
              onChange={(e) => setHolderName(e.target.value)}
              maxLength={200}
              autoComplete="cc-name"
              dir="auto"
              className={TOUCH_FIELD}
            />
          </div>
          {isCard && (
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="payment-method-month" className="text-xs text-muted-foreground">
                  {msg("billing.payment_methods.expiry_month")}
                </Label>
                <Input
                  id="payment-method-month"
                  value={month}
                  onChange={(e) => setMonth(e.target.value.replace(/\D/g, "").slice(0, 2))}
                  placeholder="MM"
                  inputMode="numeric"
                  autoComplete="cc-exp-month"
                  dir="ltr"
                  aria-invalid={!expiryValid || undefined}
                  className={cn(TOUCH_FIELD, "text-left")}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="payment-method-year" className="text-xs text-muted-foreground">
                  {msg("billing.payment_methods.expiry_year")}
                </Label>
                <Input
                  id="payment-method-year"
                  value={year}
                  onChange={(e) => setYear(e.target.value.replace(/\D/g, "").slice(0, 4))}
                  placeholder="YYYY"
                  inputMode="numeric"
                  autoComplete="cc-exp-year"
                  dir="ltr"
                  aria-invalid={!expiryValid || undefined}
                  className={cn(TOUCH_FIELD, "text-left")}
                />
              </div>
            </div>
          )}
          {!method.is_default && (
            <div className="flex items-center justify-between gap-3">
              <Label htmlFor="payment-method-default" className="text-sm text-foreground">
                {msg("billing.payment_methods.make_default")}
              </Label>
              <Switch
                id="payment-method-default"
                checked={makeDefault}
                onCheckedChange={setMakeDefault}
              />
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
              {msg("billing.payment_methods.cancel")}
            </Button>
            <Button type="submit" disabled={busy || !expiryValid} aria-busy={busy || undefined}>
              {busy && (
                <CircleNotch
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
              )}
              {msg("billing.payment_methods.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Confirm, then detach, a saved payment method. */
function RemovePaymentMethodDialog({
  method,
  onClose,
  onRemoved,
}: {
  method: PaymentMethod | null;
  onClose: () => void;
  onRemoved: (profile: BillingProfileResponse) => void;
}) {
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    setBusy(false);
  }, [method]);

  async function remove() {
    if (method == null) return;
    setBusy(true);
    try {
      onRemoved(await removePaymentMethod(method.id));
      toast.success(msg("billing.payment_methods.removed"));
      onClose();
    } catch {
      setBusy(false);
      toast.error(msg("billing.payment_methods.remove_error"));
    }
  }

  return (
    <Dialog open={method != null} onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent
        data-settings-text-buttons
        className="w-[min(28rem,92vw)] max-w-[min(28rem,92vw)] sm:max-w-md"
      >
        <DialogTitleRow
          title={msg("billing.payment_methods.remove_title")}
          description={
            method &&
            formatMsg("billing.payment_methods.remove_hint", { p1: paymentMethodLabel(method) })
          }
        />
        <DialogFooter>
          <Button type="button" variant="outline" disabled={busy} onClick={onClose}>
            {msg("billing.payment_methods.cancel")}
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={busy}
            aria-busy={busy || undefined}
            onClick={() => void remove()}
          >
            {busy && (
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            )}
            {msg("billing.payment_methods.remove")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Stripe-backed billing identity and masked saved payment methods. */
function BillingDetails() {
  const [profile, setProfile] = React.useState<BillingProfileResponse | null>(null);
  const [loadError, setLoadError] = React.useState(false);
  const [portalFlow, setPortalFlow] = React.useState<"manage" | "payment_method" | null>(null);
  const [editing, setEditing] = React.useState<PaymentMethod | null>(null);
  const [removing, setRemoving] = React.useState<PaymentMethod | null>(null);

  const loadProfile = React.useCallback(() => {
    setLoadError(false);
    getBillingProfile()
      .then(setProfile)
      .catch(() => setLoadError(true));
  }, []);

  React.useEffect(() => {
    loadProfile();
  }, [loadProfile]);

  const openPortal = React.useCallback(async (flow: "manage" | "payment_method") => {
    setPortalFlow(flow);
    try {
      const { url } = await createBillingPortalSession(flow);
      window.location.assign(url);
    } catch {
      setPortalFlow(null);
      toast.error(msg("billing.portal.error"));
    }
  }, []);

  if (loadError) {
    return (
      <div className="flex items-center justify-between gap-3 border-t border-border/40 pt-4">
        <span className="text-xs text-muted-foreground">{msg("billing.profile.load_error")}</span>
        <RetryIconButton label={msg("billing.wallet.retry")} onClick={loadProfile} />
      </div>
    );
  }

  if (profile == null) {
    return (
      <div
        className="flex h-28 items-center justify-center border-t border-border/40"
        aria-busy="true"
      >
        <CircleNotch className="size-4 animate-spin text-muted-foreground" aria-hidden="true" />
      </div>
    );
  }

  const address = formatAddress(profile.address);
  const unavailable = !profile.available;

  return (
    <div className="flex flex-col gap-6 border-t border-border/40 pt-5">
      <section className="flex flex-col gap-2" aria-labelledby="billing-profile-heading">
        <div className="flex items-center justify-between gap-3">
          <h3 id="billing-profile-heading" className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <UserCircle className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            {msg("billing.profile.title")}
          </h3>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={unavailable || portalFlow !== null}
            onClick={() => void openPortal("manage")}
            className="text-muted-foreground hover:text-foreground"
            autoTooltip={false}
            aria-label={msg("billing.profile.edit")}
          >
            {portalFlow === "manage" ? (
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <PencilSimple className="size-4" aria-hidden="true" />
            )}
          </Button>
        </div>
        <dl className="divide-y divide-border/40 border-y border-border/40">
          {[
            [msg("billing.profile.email"), profile.email],
            [msg("billing.profile.name"), profile.name],
            [msg("billing.profile.address"), address],
            [msg("billing.profile.phone"), profile.phone],
          ].map(([label, value]) => (
            <div
              key={label}
              className="grid grid-cols-1 gap-1 py-2.5 text-xs sm:grid-cols-[minmax(7rem,0.4fr)_1fr] sm:gap-4"
            >
              <dt className="text-muted-foreground">{label}</dt>
              <dd dir="auto" className="min-w-0 break-words text-foreground">
                {value || msg("billing.profile.empty")}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="flex flex-col gap-2" aria-labelledby="payment-methods-heading">
        <div className="flex items-center justify-between gap-3">
          <h3 id="payment-methods-heading" className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <CreditCard className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            {msg("billing.payment_methods.title")}
          </h3>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={unavailable || portalFlow !== null}
            onClick={() => void openPortal("payment_method")}
            className="text-muted-foreground hover:text-foreground"
            autoTooltip={false}
            aria-label={msg("billing.payment_methods.add")}
          >
            {portalFlow === "payment_method" ? (
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <Plus className="size-4" aria-hidden="true" />
            )}
          </Button>
        </div>
        {profile.payment_methods.length === 0 ? (
          <EmptyState
            variant="list"
            icon={CreditCard}
            title={
              unavailable
                ? msg("billing.profile.unavailable")
                : msg("billing.payment_methods.empty")
            }
            className="border-y border-border/40"
          />
        ) : (
          <ul className="divide-y divide-border/40 border-y border-border/40">
            {profile.payment_methods.map((method) => (
              <li key={method.id} className="flex items-center gap-3 py-3">
                <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
                  <CreditCard className="size-4" aria-hidden="true" />
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-foreground">
                    <span className="capitalize">
                      {method.brand || method.type.replaceAll("_", " ")}
                    </span>
                    {method.last4 && <span dir="ltr">•••• {method.last4}</span>}
                    {method.is_default && (
                      <Badge variant="secondary" size="sm">
                        {msg("billing.payment_methods.default")}
                      </Badge>
                    )}
                  </span>
                  {method.exp_month != null && method.exp_year != null && (
                    <span dir="ltr" className="text-xs text-muted-foreground">
                      {formatMsg("billing.payment_methods.expires", {
                        p1: `${String(method.exp_month).padStart(2, "0")}/${String(method.exp_year).slice(-2)}`,
                      })}
                    </span>
                  )}
                </span>
                <span className="flex shrink-0 items-center gap-0.5">
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    disabled={unavailable}
                    onClick={() => setEditing(method)}
                    className="text-muted-foreground hover:text-foreground"
                    autoTooltip={false}
                    aria-label={`${msg("billing.payment_methods.edit")} ${paymentMethodLabel(method)}`}
                  >
                    <PencilSimple className="size-4" aria-hidden="true" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    disabled={unavailable}
                    onClick={() => setRemoving(method)}
                    className="text-muted-foreground hover:text-destructive"
                    autoTooltip={false}
                    aria-label={`${msg("billing.payment_methods.remove")} ${paymentMethodLabel(method)}`}
                  >
                    <Trash className="size-4" aria-hidden="true" />
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <EditPaymentMethodDialog
        method={editing}
        onClose={() => setEditing(null)}
        onSaved={setProfile}
      />
      <RemovePaymentMethodDialog
        method={removing}
        onClose={() => setRemoving(null)}
        onRemoved={setProfile}
      />
    </div>
  );
}

/**
 * Wallet — the `billing` settings tab.
 *
 * A calm, left-aligned balance block (not a centered hero metric) and the
 * add-funds row. Billing details, payment methods, and transaction history
 * follow below. Balances are stored in cents and read in dollars.
 */
export function WalletTab() {
  const { shown, syncing, loading, loadError, refresh } = useBalance();
  const { totalCents, status, available, linkNeeded } = shown;
  const { locale } = useLocale();

  return (
    <div className="flex flex-col gap-5">
      {loadError && (
        <InlineErrorRow
          message={msg("billing.wallet.load_error")}
          className="items-center py-2"
          action={
            <RetryIconButton
              label={msg("billing.wallet.retry")}
              loading={loading}
              onClick={refresh}
            />
          }
        />
      )}
      <section className="flex flex-col gap-5">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-col gap-1">
            <span className="flex items-center gap-1.5 text-[0.6875rem] font-semibold uppercase tracking-widest text-muted-foreground">
              <Wallet className="size-3.5 shrink-0" aria-hidden="true" />
              {msg("billing.popover.title")}
            </span>
            <div className="flex items-center gap-2" aria-busy={syncing || undefined}>
              {/* Keep the prior balance visible until the checkout webhook arrives. */}
              <span
                dir="ltr"
                className={cn(
                  "text-3xl font-semibold text-foreground tabular-nums",
                  syncing && "animate-pulse text-muted-foreground",
                )}
              >
                {available ? formatCentsUsd(totalCents, locale) : "—"}
              </span>
            </div>
            {linkNeeded && <LinkAccountPrompt />}
            {available && status === "low" && (
              <span className="text-xs text-muted-foreground/80">
                {msg("billing.chip.low_note")}
              </span>
            )}
          </div>
        </div>

        <div>
          <AddFundsControls />
        </div>
      </section>

      <BillingDetails />
      <TransactionHistory />
    </div>
  );
}
