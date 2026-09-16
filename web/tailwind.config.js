/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      colors: {
        accent: {
          DEFAULT: '#0071E3',
          hover: '#0077ED',
          press: '#0068D1',
        },
        canvas: '#F5F5F7',
        card: '#FFFFFF',
        ink: {
          DEFAULT: '#1D1D1F',
          2: '#6E6E73',
          3: '#86868B',
        },
        danger: '#FF3B30',
        success: '#34C759',
        'bubble-in': '#E9E9EB',
        fill: 'rgba(120, 120, 128, 0.12)',
        'fill-2': 'rgba(120, 120, 128, 0.16)',
        hairline: 'rgba(0, 0, 0, 0.06)',
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          'SF Pro Text',
          'SF Pro Display',
          'Inter',
          'Helvetica Neue',
          'system-ui',
          'sans-serif',
        ],
      },
      fontSize: {
        caption: ['12px', { lineHeight: '16px', letterSpacing: '0.01em', fontWeight: '400' }],
        body: ['15px', { lineHeight: '21px', fontWeight: '400' }],
        title: ['17px', { lineHeight: '22px', fontWeight: '600' }],
        ltitle: ['28px', { lineHeight: '34px', letterSpacing: '-0.02em', fontWeight: '600' }],
      },
      borderRadius: {
        card: '16px',
      },
      boxShadow: {
        float: '0 8px 24px rgba(0,0,0,0.12), 0 2px 8px rgba(0,0,0,0.08)',
        seg: '0 1px 3px rgba(0,0,0,0.1), 0 1px 1px rgba(0,0,0,0.06)',
      },
      transitionTimingFunction: {
        apple: 'cubic-bezier(0.25, 0.1, 0.25, 1)',
      },
      maxWidth: {
        app: '432px',
      },
    },
  },
  plugins: [],
}
