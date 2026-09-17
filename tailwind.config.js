/**
 * Tailwind CSS build configuration for ssavevideo.com.
 *
 * The production HTML references only the compiled, purged artifact at
 * `app/static/css/app.css`, so the site ships zero JavaScript CSS runtime
 * (no cdn.tailwindcss.com, no FOUC, works under a strict CSP).
 *
 * `data/seo_platforms.json` is part of `content` on purpose: every brand
 * gradient in the pSEO schema (`color_gradient`, `chip_class`, ...) is a
 * complete literal Tailwind class string, so the JIT scanner keeps it even
 * though it is applied from JSON at request time instead of from the HTML.
 */
module.exports = {
  content: [
    './app/templates/**/*.html',
    './app/static/js/**/*.js',
    './data/*.json',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter var', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'Noto Sans', 'sans-serif'],
      },
      maxWidth: {
        '8xl': '90rem',
      },
      keyframes: {
        'fade-up': {
          '0%': { opacity: '0', transform: 'translateY(10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-500px 0' },
          '100%': { backgroundPosition: '500px 0' },
        },
      },
      animation: {
        'fade-up': 'fade-up .45s cubic-bezier(.16,1,.3,1) both',
        shimmer: 'shimmer 1.4s linear infinite',
      },
    },
  },
  plugins: [],
};
