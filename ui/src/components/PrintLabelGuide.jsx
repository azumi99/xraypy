import windowsForms from "../assets/print-guide/windows-forms.png";
import zebraGraphics from "../assets/print-guide/zebra-graphics.png";
import zebraPageSetup from "../assets/print-guide/zebra-page-setup.png";

function GuidePhoto({ src, alt, caption }) {
  return <figure className="m-0">
    <a href={src} target="_blank" rel="noreferrer">
      <img src={src} alt={alt} width="1204" height="1600" loading="lazy"
        className="block w-full max-w-[360px] h-auto rounded-lg border border-[var(--border)]" />
    </a>
    <figcaption className="mt-2 text-xs text-muted">{caption} Klik foto untuk memperbesar.</figcaption>
  </figure>;
}

export default function PrintLabelGuide({ labelsPath }) {
  return <article className="grid gap-4 max-w-4xl">
    <div><a className="btn-ghost inline-flex" href={labelsPath}>← Kembali ke Print Label</a></div>
    <section className="panel grid gap-3">
      <h3>1. Buat ukuran label 100 × 50 mm</h3>
      <ol className="list-decimal pl-5 space-y-2">
        <li>Di Windows, buka <strong>Printers &amp; scanners</strong>, pilih <strong>Zebra ZT230 (203 dpi)</strong>, lalu buka <strong>Printing Preferences</strong>.</li>
        <li>Pada tab <strong>Page Setup</strong>, klik <strong>New…</strong> di bagian <strong>Stock</strong>.</li>
        <li>Simpan ukuran dengan nama <strong>XRAY CARGO 100x50</strong>. Isi Width <strong>100 mm</strong> (3,94 in) dan Height <strong>50 mm</strong> (1,97 in), lalu pilih ukuran tersebut.</li>
        <li>Mulai dengan orientasi <strong>Portrait</strong> di driver. Saat mencetak, pastikan preview akhir melebar. Biarkan <strong>Mirror Image</strong> dan <strong>Negative</strong> tidak dicentang.</li>
      </ol>
      <GuidePhoto src={zebraPageSetup} alt="Tab Page Setup pada Zebra ZT230 Printing Preferences"
        caption="Foto menunjukkan Stock awal 4 × 4 in. Gunakan XRAY CARGO 100x50 saat mencetak." />
    </section>
    <section className="panel grid gap-3">
      <h3>2. Periksa resolusi dan ukuran kertas</h3>
      <p>Di tab <strong>Graphics</strong>, pilih <strong>203 dpi × 203 dpi</strong> sesuai model printer.</p>
      <p>Jika ukuran belum muncul di dialog cetak, buka <strong>Print Server Properties → Forms</strong>. Centang <strong>Create a new form</strong>, masukkan ukuran 100 × 50 mm, atur margin ke 0 jika driver mengizinkan, lalu klik <strong>Save Form</strong>. Buka ulang dialog cetak.</p>
      <div className="grid gap-4 sm:grid-cols-2">
        <GuidePhoto src={zebraGraphics} alt="Tab Graphics dengan resolusi 203 dpi"
          caption="Pilihan resolusi di tab Graphics." />
        <GuidePhoto src={windowsForms} alt="Tab Forms di Windows Print Server Properties"
          caption="Lokasi pengaturan ukuran di Windows. Ganti ukuran pada contoh layar dengan 100 × 50 mm." />
      </div>
    </section>
    <section className="panel grid gap-3">
      <h3>3. Cetak satu label untuk tes</h3>
      <p>Pilih satu label di Print Label, klik <strong>Print</strong>, lalu gunakan pengaturan berikut di dialog browser.</p>
      <ul className="list-disc pl-5 space-y-1">
        <li><strong>Printer</strong> Zebra ZT230</li>
        <li><strong>Paper size</strong> XRAY CARGO 100x50</li>
        <li><strong>Scale</strong> 100% atau Actual size</li>
        <li><strong>Margins</strong> None</li>
        <li><strong>Headers and footers</strong> Off</li>
      </ul>
      <p>Pastikan preview menampilkan satu label per halaman dan seluruh QR serta barcode terlihat. Cetak satu label dan uji scan sebelum mencetak banyak.</p>
      <p>Jika printer melewati stiker atau posisi cetak bergeser, periksa jenis media. Untuk roll bercelah, pilih <strong>Gap/Web</strong> jika tersedia.</p>
    </section>
  </article>;
}
