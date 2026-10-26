// Scaffold — frontend teammate replaces with the real router / layouts / pages.
import { Navigate, Route, Routes } from 'react-router-dom'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<div style={{ padding: 48 }}>Echoic 前端脚手架已就绪</div>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
