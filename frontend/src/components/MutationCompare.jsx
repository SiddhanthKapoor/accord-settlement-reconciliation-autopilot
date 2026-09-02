import { motion } from "motion/react";
import { moneyStr } from "../scenarios.js";

export default function MutationCompare({ verified, observed }) {
  const qtyDiff = verified.qty !== observed.qty;
  const priceDiff = verified.price !== observed.price;
  return (
    <motion.div className="compare-grid" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.2 }}>
      <div className="compare-col">
        <div className="compare-col-label">Verified state</div>
        <div className="compare-line">
          <span className="compare-line-label">Product</span>
          <span className="compare-line-value">{verified.label}</span>
        </div>
        <div className="compare-line">
          <span className="compare-line-label">Quantity</span>
          <span className="compare-line-value">{verified.qty}</span>
        </div>
        <div className="compare-line">
          <span className="compare-line-label">Amount</span>
          <span className="compare-line-value">{moneyStr(verified.price)}</span>
        </div>
      </div>
      <div className="compare-vs">→</div>
      <div className="compare-col">
        <div className="compare-col-label">Observed payment state</div>
        <div className="compare-line">
          <span className="compare-line-label">Product</span>
          <span className="compare-line-value">{observed.label}</span>
        </div>
        <div className={"compare-line" + (qtyDiff ? " compare-diff" : "")}>
          <span className="compare-line-label">Quantity</span>
          <span className="compare-line-value">{qtyDiff && "⚠ "}{observed.qty}</span>
        </div>
        <div className={"compare-line" + (priceDiff ? " compare-diff" : "")}>
          <span className="compare-line-label">Amount</span>
          <span className="compare-line-value">{priceDiff && "⚠ "}{moneyStr(observed.price)}</span>
        </div>
      </div>
    </motion.div>
  );
}
