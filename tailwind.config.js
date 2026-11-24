/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./apps/**/templates/**/*.html",
    "./static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#FAF4EB',
          100: '#F5EBDD',
          200: '#E8DED4',
          300: '#D5C2B1',
          400: '#A88D7E',
          500: '#8A7B72',
          600: '#702C3B',
          700: '#702C3B',
          800: '#57212E',
          900: '#49372F',
          950: '#230E14',
        },
        accent: {
          500: '#702C3B',
          600: '#57212E',
        },
        surface: {
          bg: '#F5EBDD',
          card: '#FFFFFF',
          border: '#E8DED4',
          heading: '#49372F',
          secondary: '#8A7B72',
        },
        coffee: {
          50: '#FDFBF7',
          100: '#F5EBDD',
          200: '#E8DED4',
          300: '#D5C2B1',
          400: '#A88D7E',
          500: '#8A7B72',
          600: '#702C3B',
          700: '#5C2330',
          800: '#491C26',
          900: '#3B1720',
          950: '#230E14',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
      },
      boxShadow: {
        'card': '0 1px 3px 0 rgba(0, 0, 0, 0.05), 0 1px 2px -1px rgba(0, 0, 0, 0.05)',
        'dropdown': '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.05)',
        'modal': '0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.05)',
      },
    },
  },
  plugins: [],
}
