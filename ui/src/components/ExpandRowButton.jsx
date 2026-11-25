export default function ExpandRowButton({ expanded, label, onToggle }) {
  return (
    <button type="button" className="table-expand" aria-label={label} aria-expanded={expanded}
      onClick={(event) => { event.stopPropagation(); onToggle(); }}>
      <svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" fill="none"
        stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M4 8h8" />
        {!expanded && <path d="M8 4v8" />}
      </svg>
    </button>
  );
}
