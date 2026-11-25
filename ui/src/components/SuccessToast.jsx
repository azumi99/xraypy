import { useEffect } from "react";
import { createPortal } from "react-dom";

export default function SuccessToast({ title, children, onClose }) {
  useEffect(() => {
    const timeout = setTimeout(onClose, 60_000);
    return () => clearTimeout(timeout);
  }, [onClose]);

  return createPortal(
    <div className="success-toast">
      <div role="status">
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
      <button type="button" aria-label="Tutup notifikasi" onClick={onClose}>×</button>
    </div>,
    document.body,
  );
}
