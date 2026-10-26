import { useEffect } from 'react'
import { Link } from 'react-router-dom'

export function NotFoundPage() {
  useEffect(() => {
    document.title = '页面不存在 · Echoic'
  }, [])

  return (
    <div className="notfound-page">
      <div className="notfound-page__code">404</div>
      <div className="notfound-page__title">页面走丢了</div>
      <div className="notfound-page__desc">你访问的页面不存在，或者已经被移动。</div>
      <Link to="/" className="notfound-page__link">
        返回资料库
      </Link>
    </div>
  )
}
