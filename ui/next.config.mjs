/** @type {import('next').NextConfig} */
const nextConfig = {
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
  turbopack: {
    resolveAlias: {
      // jsPDF (used only for the PDF report export, client-side) resolves to its Node
      // build under SSR, which pulls fflate's node worker (`new Worker(<dynamic>)`) and
      // breaks Turbopack. Force the browser builds — jsPDF only calls the synchronous
      // fflate `zlibSync`, so no worker is ever spawned.
      jspdf: 'jspdf/dist/jspdf.es.min.js',
      fflate: 'fflate/esm/browser.js',
    },
  },
}

export default nextConfig
