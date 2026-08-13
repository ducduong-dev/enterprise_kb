/**
 * Portal shell.
 *
 * Screens land per milestone: upload and registry (M1), search (M2), review editor (M3),
 * merge 3-pane and approvals (M5), admin (M5+). Every screen authenticates through Keycloak
 * and calls portal-api; nothing in the browser holds an ACL decision.
 */
import { useEffect, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import { User } from "oidc-client-ts";
import { completeSignIn, displayName, getUser, signIn, signOut } from "./auth";
import { ApiError, Category, listCategories } from "./api";
import { Documents } from "./pages/Documents";
import { DocumentDetail } from "./pages/DocumentDetail";
import { ReviewQueue } from "./pages/ReviewQueue";
import { ReviewEditor } from "./pages/ReviewEditor";
import { MergeEditor } from "./pages/MergeEditor";
import { Chat } from "./pages/Chat";
import { Search } from "./pages/Search";
import { Upload } from "./pages/Upload";
import "./styles.css";

function ReviewEditorRoute({ categories }: { categories: Category[] }) {
  const { taskId } = useParams();
  const navigate = useNavigate();
  if (!taskId) return <p className="page">Không tìm thấy việc cần duyệt.</p>;
  return (
    <ReviewEditor
      taskId={taskId}
      categories={categories}
      onDone={() => navigate("/review")}
    />
  );
}

function MergeEditorRoute() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  if (!taskId) return <p className="page">Không tìm thấy việc hợp nhất.</p>;
  return <MergeEditor taskId={taskId} onDone={() => navigate("/review")} />;
}

function DocumentDetailRoute() {
  const { documentId } = useParams();
  if (!documentId) return <p className="page">Không tìm thấy tài liệu.</p>;
  return <DocumentDetail documentId={documentId} />;
}

function Callback() {
  useEffect(() => {
    completeSignIn()
      .then(() => window.location.replace("/"))
      .catch(() => window.location.replace("/"));
  }, []);
  return <p className="page">Đang hoàn tất đăng nhập…</p>;
}

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  /**
   * Why sign-in failed, if it did. `signinRedirect()` fetches the provider's metadata before
   * it navigates anywhere, so a provider the browser cannot reach makes the button look
   * broken rather than failing — the click is handled, the promise rejects, nothing moves.
   */
  const [authError, setAuthError] = useState<string | null>(null);
  /** Same failure mode one screen along: a category tree that never arrives leaves the upload
   *  form and the search facets empty, which reads as "the bank has no categories". */
  const [dataError, setDataError] = useState<string | null>(null);

  useEffect(() => {
    getUser().then(setUser);
    // The category tree drives both the upload form's defaults and the search facets.
    listCategories()
      .then((rows) => {
        setCategories(rows);
        setDataError(null);
      })
      .catch((error: ApiError) => {
        setCategories([]);
        // 401 before sign-in is expected: the tree loads on the reload that follows it.
        if (error.status !== 401) {
          setDataError(`Không tải được danh mục (${error.message})`);
        }
      });
  }, []);

  return (
    <BrowserRouter>
      <header className="topbar">
        <span className="brand">Cơ sở tri thức</span>
        <nav>
          <Link to="/search">Tìm kiếm</Link>
          <Link to="/chat">Hỏi đáp</Link>
          <Link to="/upload">Tải lên</Link>
          <Link to="/documents">Sổ đăng ký</Link>
          <Link to="/review">Việc cần duyệt</Link>
        </nav>
        <span className="user">
          {displayName(user)}
          {user ? (
            <button onClick={() => void signOut()}>Đăng xuất</button>
          ) : (
            <button
              onClick={() =>
                signIn().catch((error: unknown) =>
                  setAuthError(
                    `Không kết nối được máy chủ đăng nhập (${
                      error instanceof Error ? error.message : String(error)
                    })`,
                  ),
                )
              }
            >
              Đăng nhập
            </button>
          )}
        </span>
      </header>
      {authError && (
        <p className="error page" role="alert">
          {authError}
        </p>
      )}
      {dataError && (
        <p className="error page" role="alert">
          {dataError}
        </p>
      )}
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/search" replace />} />
          <Route path="/search" element={<Search categories={categories} />} />
          <Route path="/chat" element={<Chat categories={categories} />} />
          <Route path="/upload" element={<Upload />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/documents/:documentId" element={<DocumentDetailRoute />} />
          <Route path="/review" element={<ReviewQueue />} />
          <Route
            path="/review/:taskId"
            element={<ReviewEditorRoute categories={categories} />}
          />
          <Route path="/merge/:taskId" element={<MergeEditorRoute />} />
          <Route path="/callback" element={<Callback />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}
