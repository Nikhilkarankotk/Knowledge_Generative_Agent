import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { App } from './app';
import { Api } from './services/api/api';

describe('App', () => {
  let getTopExperts: ReturnType<typeof vi.spyOn>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
    }).compileComponents();

    const api = TestBed.inject(Api);
    getTopExperts = vi
      .spyOn(api, 'getTopExperts')
      .mockReturnValue(of({ chatMessageId: 1, experts: [] }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(App);
    const app = fixture.componentInstance;
    expect(app).toBeTruthy();
  });

  it('should render the app shell', async () => {
    const fixture = TestBed.createComponent(App);
    await fixture.whenStable();
    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.app-container')).toBeTruthy();
  });

  it('should render experts returned by the API', () => {
    getTopExperts.mockReturnValue(
      of({
        chatMessageId: 1,
        experts: [
          {
            name: 'Alice Doe',
            role: 'Owner',
            source: 'Confluence',
            sourceCount: 1,
            reason: 'Owner of Payments Architecture',
            initials: 'AD',
          },
        ],
      }),
    );

    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.expert-row')?.textContent).toContain('Alice Doe');
    expect(compiled.querySelector('.experts-empty')).toBeFalsy();
  });

  it('should show the empty state when no experts are found', () => {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.experts-empty')?.textContent).toContain(
      'No experts identified for this topic',
    );
  });
});
